# BestPlan Grounded Quality and Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the already-approved compact BestPlan response, capture truthful per-model runtime telemetry, and add budget-matched experiments for grounded candidates and operational explorer lenses without changing live Hermes state.

**Architecture:** Keep routing, identities, quorum, synthesis selection, and receipts host-owned in `agent.bestplan_orchestrator`. Put compact rendering, candidate/evidence validation, and telemetry projection in small focused modules. Preserve receipt V2 during the experiment; promote a grounded mode and design receipt V3 only after measured acceptance gates pass.

**Tech Stack:** Python 3.11-3.13, pytest through `scripts/run_tests.sh`, JSON/YAML configuration, Hermes `AIAgent` usage accounting, GitNexus

---

## Governing specifications

- `/Users/seb/hermes-webui/docs/superpowers/specs/2026-07-24-bestplan-compact-output-design.md`
- `/Users/seb/hermes-webui/docs/superpowers/specs/2026-07-24-bestplan-grounded-exploration-design.md`
- `/Users/seb/hermes-webui/docs/superpowers/specs/2026-07-24-bestplan-generic-explorer-pool-kimi-k3-design.md`

If this plan conflicts with a governing specification, stop and reconcile the
specification before editing product code.

## Repositories and safety boundary

- Design and plan documents live in `/Users/seb/hermes-webui`.
- Agent implementation starts from commit
  `a62e8c2c1ba53827a7d3df88efa547dd85bf97dd` and must use a fresh isolated
  Hermes Agent worktree and a `codex/` branch.
- Suggested worktree:
  `/Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-quality`.
- Use `scripts/run_tests.sh`; never invoke bare `pytest` or install test
  packages into a system interpreter.
- Do not modify `~/.hermes/config.yaml`, `~/.hermes/.env`, provider
  credentials, receipts, sessions, or any other live state.
- Do not run the paid evaluation corpus, restart, kill, deploy, or activate
  Hermes/WebUI without a separate explicit instruction.
- Experimental Candidate V2/lens evaluation must fail preflight when any
  scheduled runtime uses `codex_app_server` or resolved xAI Responses. Those
  paths can bypass Hermes' scoped/frozen tool adapter and are not falsely
  treated as contained or request-counted.
- Never place a real API key, raw provider response, endpoint authorization
  data, private task text, or unredacted evidence in a prompt fixture, log,
  receipt, diff, or commit.
- The key previously pasted into chat is exposed. Do not use it. Rotate it
  before any later live provider probe.
- Before editing a function, class, method, or catalog object, Codex runs
  GitNexus upstream impact and records the risk. Stop and warn before a HIGH or
  CRITICAL edit.
- Before every commit, run GitNexus `detect_changes` on the staged scope and
  inspect the exact diff.

## Execution ownership

Use local Ornith for bounded test and implementation edits. Codex retains
scope, GitNexus impact checks, review, integration, test execution, secret
inspection, and acceptance. Do not expose sub-agent orchestration telemetry in
user-visible chat.

## File structure

### New files

- `agent/bestplan_presentation.py`
  - ledger token sanitization;
  - host-owned model/status ledger;
  - compact body validation; and
  - bounded reformat prompt construction.
- `agent/bestplan_telemetry.py`
  - allowlisted projection of one child result;
  - reported, partial, and unavailable usage semantics; and
  - qualified cost serialization.
- `agent/bestplan_candidate.py`
  - V1/V2 experiment-aware candidate parsing;
  - V2 structural and claim-link validation;
  - evidence-locator admission;
  - fail-closed bounded evaluation canonicalization and private sink;
  - anonymous synthesis envelopes; and
  - operational lens definitions.
- `agent/bestplan_inspection.py`
  - immutable scoped task registry and reserved-prefix fail-closed policy;
  - typed frozen evaluation I/O adapter; and
  - canonical evidence-admission provenance helpers shared by host/tools.
- `scripts/evaluate_bestplan.py`
  - owner-only evaluation initialization, design-manifest hashing, pre-pilot
    holdout freezing, and frozen-source preparation;
  - isolated, resumable 2-by-2 evaluation runner;
  - separately gated, count-matched five-lens ablation runner;
  - durable pre-dispatch debit/result journaling;
  - blinded condition IDs;
  - bounded JSONL output; and
  - paired metric summaries.
- `scripts/bestplan_eval_checks.py`
  - closed versioned executable-check registry;
  - read-only/no-network deterministic sandbox runner; and
  - canonical invocation/result attestations.
- `tests/agent/test_bestplan_presentation.py`
- `tests/agent/test_bestplan_telemetry.py`
- `tests/agent/test_bestplan_candidate.py`
- `tests/agent/test_usage_provenance.py`
- `tests/scripts/test_evaluate_bestplan.py`
- `tests/fixtures/bestplan_eval_cases.example.jsonl`
  - synthetic/redacted schema examples only.
- `tests/fixtures/bestplan_eval_results.example.jsonl`
  - one complete synthetic blinded four-condition block.
- `tests/fixtures/bestplan_eval_phase_a_bundle.example.jsonl`
- `tests/fixtures/bestplan_eval_phase_a_judgments.example.jsonl`
- `tests/fixtures/bestplan_eval_phase_a_consensus.example.jsonl`
- `tests/fixtures/bestplan_eval_phase_b_bundle.example.jsonl`
- `tests/fixtures/bestplan_eval_phase_b_judgments.example.jsonl`
- `tests/fixtures/bestplan_eval_phase_b_consensus.example.jsonl`
  - synthetic two-phase blinding, freeze, duplicate-score, and adjudication
    examples only.
- `tests/fixtures/bestplan_eval_positive_summary.example.json`
  - synthetic positive 2-by-2 prerequisite for ablation dry-run tests only.
- `tests/fixtures/bestplan_eval_runtime.example.yaml`
  - synthetic secret-free isolated host-mediated pool/profile.
- `tests/fixtures/bestplan_eval_sampling_frame.example.json`
  - synthetic pre-pilot family-stratified holdout with cutoff/provenance
    digests and deterministic selection order.
- `tests/fixtures/bestplan_eval_sampling_frame.example.sha256`
- `tests/fixtures/bestplan_eval_design.example.json`
  - synthetic full experiment-design manifest and stable digest fixture.
- `tests/fixtures/bestplan_eval_design.example.sha256`
- `tests/fixtures/bestplan_eval_cost_bounds.example.json`
  - synthetic qualified tariff-derived upper-bound manifest.
- `tests/fixtures/bestplan_eval_cost_bounds.example.sha256`
- `tests/fixtures/bestplan_eval_check_attestation.example.json`
  - synthetic immutable executable-check invocation/result attestation.
- `tests/fixtures/bestplan_eval_source_input.example.json`
  - synthetic approved task/web/file-source preparation input.
- `tests/fixtures/bestplan_eval_source_map.example.json`
  - synthetic exact task/web provenance plus approved snapshot-backed file
    ranges used only in isolated harness tests.
- `tests/fixtures/bestplan_eval_source_corpus.example.json`
  - synthetic owner-only visible-fact, frozen canonical-URL, and file-range
    adapter corpus.
- `tests/fixtures/bestplan_eval_workspaces/synthetic-001/`
  - tiny redacted immutable workspace fixture and exact content manifest.

### Existing files

- `agent/bestplan_orchestrator.py`
  - integrates the three focused modules;
  - retains routing, scheduling, timeout, teardown, quorum, and receipt
    ownership.
- `agent/conversation_loop.py`
  - projects the structured receipt field and bounded telemetry result without
    parsing visible output.
- `agent/chat_completion_helpers.py`
  - records the initial and retry max-iteration provider calls separately in
    the optional BestPlan accounting ledger.
- `agent/codex_runtime.py`
  - reports explicit usage presence on the Codex app-server early-return path
    without conflating auxiliary compaction accounting.
- `agent/agent_init.py`
  - initializes explicit provider-usage provenance.
- `agent/turn_finalizer.py`
  - returns explicit provider-usage provenance with canonical counters.
- `run_agent.py`
  - resets provider-usage provenance with the other session counters.
- `tools/file_tools.py`
  - enforces an immutable realpath-confined read/search scope for registered
    BestPlan child task IDs only.
- `tools/web_tools.py`
  - routes evaluation-prefixed search/extract calls through the frozen private
    adapter and forbids live fallback.
- `model_tools.py`
  - receives the construction-time task ID, exposes exact canonical frozen
    tool schemas, and bypasses mutable registry/plugin hooks for evaluation
    task IDs only.
- `hermes_cli/config.py`
  - fixes the invalid checked-in BestPlan default and adds strict experiment
    defaults.
- `hermes_cli/subcommands/bestplan.py`
  - shows normalized experiment modes without running models.
- `cli-config.yaml.example`
  - documents the two independent experiment switches.
- `tests/agent/test_bestplan_orchestrator.py`
- `tests/agent/test_conversation_loop_bestplan.py`
- `tests/tools/test_bestplan_read_scope.py`
- `tests/tools/test_bestplan_evaluation_io.py`
- `tests/test_model_tools.py`
- `tests/hermes_cli/test_bestplan_cli.py`
- `tests/run_agent/test_codex_app_server_integration.py`
- `website/docs/reference/cli-commands.md`
- `website/docs/reference/slash-commands.md`
- `website/docs/user-guide/configuration.md`

Do not split provider resolution, K3 trust checks, receipt persistence, or
teardown into new modules during this change.

### Task 1: Create the isolated implementation worktree

**Files:**

- No product files

- [ ] **Step 1: Inspect the completed K3 source worktree**

Run:

```bash
git -C /Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3 status --short --branch
git -C /Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3 rev-parse HEAD
```

Expected: clean branch `codex/bestplan-kimi-k3` at
`a62e8c2c1ba53827a7d3df88efa547dd85bf97dd`.

- [ ] **Step 2: Use the worktree skill**

Invoke `@superpowers:using-git-worktrees` and create
`codex/bestplan-quality` from the exact commit above at the suggested path.
Do not reuse or mutate the completed K3 worktree.

- [ ] **Step 3: Verify isolation**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git worktree list
```

Expected: the new worktree is clean, on `codex/bestplan-quality`, at the
expected base commit.

- [ ] **Step 4: Run the focused baseline**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_bestplan_orchestrator.py \
  tests/agent/test_conversation_loop_bestplan.py \
  tests/hermes_cli/test_bestplan_cli.py -q
```

Expected: PASS. Record the test count and duration as the implementation
baseline.

### Task 2: Repair the checked-in BestPlan default

**Files:**

- Modify: `hermes_cli/config.py`
- Modify: `tests/hermes_cli/test_bestplan_cli.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`

- [ ] **Step 1: Pass the graph gate**

Run GitNexus upstream impact for `DEFAULT_CONFIG`, `validate_runtime`, and
`cmd_bestplan`. Record direct callers and risk before delegation.

- [ ] **Step 2: Write the failing default-validation test**

Add a test equivalent to:

```python
def test_checked_in_bestplan_default_validates():
    from agent.bestplan_orchestrator import validate_runtime
    from hermes_cli.config import DEFAULT_CONFIG

    resolved = validate_runtime(DEFAULT_CONFIG["bestplan"])

    assert [entry["name"] for entry in resolved["explorers"]] == ["glm", "sol"]
    assert resolved["synthesizer"] == "sol"
```

Add a CLI test proving `hermes bestplan lanes` reports `Validation: PASS`
when `load_config()` returns the checked-in default.

- [ ] **Step 3: Run the RED tests**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_bestplan_orchestrator.py \
  tests/hermes_cli/test_bestplan_cli.py -q
```

Expected: FAIL because the checked-in config names synthesizer `strongest`,
which is not a configured explorer.

- [ ] **Step 4: Implement the minimal default correction**

In `hermes_cli/config.py`:

- express the checked-in block with canonical `explorers`;
- set `synthesizer: "sol"`;
- preserve the current GLM/Sol model identities and timeout values; and
- do not add Kimi credentials or modify any live config.

Do not weaken named-synthesizer validation to accept the magic value
`strongest`.

- [ ] **Step 5: Run the GREEN tests**

Run the command from Step 3.

Expected: PASS.

- [ ] **Step 6: Stage and inspect**

Run:

```bash
git add hermes_cli/config.py \
  tests/hermes_cli/test_bestplan_cli.py \
  tests/agent/test_bestplan_orchestrator.py
git diff --cached
```

Run GitNexus staged `detect_changes`. Confirm provider resolution, K3 trust
checks, and live state are untouched.

- [ ] **Step 7: Commit**

Run:

```bash
git commit -m "fix(bestplan): validate checked-in runtime defaults"
```

### Task 3: Implement compact visible output

**Files:**

- Create: `agent/bestplan_presentation.py`
- Create: `tests/agent/test_bestplan_presentation.py`
- Modify: `agent/bestplan_orchestrator.py`
- Modify: `agent/conversation_loop.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`
- Modify: `tests/agent/test_conversation_loop_bestplan.py`

- [ ] **Step 1: Pass the graph gate**

Run GitNexus upstream impact for `run_bestplan`, `make_receipt`, and the
BestPlan command path in `conversation_loop.py`. Report before editing if any
risk is HIGH or CRITICAL.

- [ ] **Step 2: Write ledger sanitizer tests**

In `tests/agent/test_bestplan_presentation.py`, add tests proving:

- scheduled order is preserved;
- resolved identity is preferred;
- configured identity is used only when resolution is null;
- the named synthesizer is explicit;
- status and allowlisted reason codes are host-owned;
- newlines, controls, Markdown delimiters, and non-allowlisted characters
  become `?`;
- whitespace collapses; and
- every identity token is at most 64 characters.

Use a sentinel such as `SENTINEL_SECRET` only to prove that raw error text is
never rendered.

- [ ] **Step 3: Write compact body contract tests**

Add table-driven tests for:

```text
TL;DR
- two to four bullets

Next steps
1. one to five items

Risks
- zero to three items
```

Test exact headings, optional omission of `Risks`, per-line 240-character
limits, total 2,000-character limit, no preamble, no second model ledger, and
no receipt markers.

- [ ] **Step 4: Write orchestration RED tests**

Add tests proving:

- first invalid synthesis triggers exactly one reformat call through the same
  named synthesizer;
- a second invalid result fails with `synthesizer_failed` and no plan body;
- successful `final_response` contains only the host ledger and compact body;
- successful and failed structured results still contain `receipt`;
- the finalized BestPlan host result carries the exact structured receipt
  string for post-validation success and failure;
- persisted canonical receipt JSON equals the structured receipt JSON; and
- finalized/streamed chat contains no receipt marker or raw receipt JSON.

- [ ] **Step 5: Run the RED tests**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_bestplan_presentation.py \
  tests/agent/test_bestplan_orchestrator.py \
  tests/agent/test_conversation_loop_bestplan.py -q
```

Expected: new presentation tests fail because the module and compact/reformat
path do not yet exist.

- [ ] **Step 6: Implement presentation helpers**

Implement small pure helpers with signatures equivalent to:

```python
def sanitize_ledger_token(value: object, *, limit: int = 64) -> str: ...

def format_model_ledger(
    attempts: list[dict[str, object]],
    synthesizer: dict[str, object],
) -> str: ...

def validate_compact_body(body: str) -> str: ...

def build_reformat_prompt(invalid_body: str) -> str: ...
```

The helpers receive only bounded metadata. They never inspect runtime objects
or provider exceptions.

- [ ] **Step 7: Integrate one bounded reformat retry**

In `run_bestplan`:

- retain the full V2 receipt in the mandatory structured `receipt` field;
- remove it from successful `final_response`;
- validate the first synthesizer body;
- run exactly one same-synthesizer-runtime reformat attempt in a fresh child
  instance when invalid, with all bounded context in the retry prompt;
- classify retry timeout/provider/format failure as `synthesizer_failed`;
- never truncate an invalid plan; and
- preserve the existing receipt persistence warning.

In `conversation_loop.py`, consume the structured `receipt`; never recover it
by parsing visible response text. After `finalize_turn()` returns, attach the
exact marker-wrapped `outcome["receipt"]` to the finalized result for every
post-validation success or failure. Do not place the marker in
`final_response`, persisted assistant content, or streamed text.

- [ ] **Step 8: Run the GREEN tests**

Run the command from Step 5.

Expected: PASS.

- [ ] **Step 9: Stage, inspect, and commit**

Run:

```bash
git add agent/bestplan_presentation.py \
  agent/bestplan_orchestrator.py \
  agent/conversation_loop.py \
  tests/agent/test_bestplan_presentation.py \
  tests/agent/test_bestplan_orchestrator.py \
  tests/agent/test_conversation_loop_bestplan.py
git diff --cached
```

Run GitNexus staged `detect_changes`, then commit:

```bash
git commit -m "feat(bestplan): render compact model-aware plans"
```

### Task 4: Capture allowlisted child telemetry without changing receipts

**Files:**

- Create: `agent/bestplan_telemetry.py`
- Create: `tests/agent/test_bestplan_telemetry.py`
- Create: `tests/agent/test_usage_provenance.py`
- Modify: `agent/bestplan_orchestrator.py`
- Modify: `agent/conversation_loop.py`
- Modify: `agent/chat_completion_helpers.py`
- Modify: `agent/codex_runtime.py`
- Modify: `agent/agent_init.py`
- Modify: `agent/turn_finalizer.py`
- Modify: `run_agent.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`
- Modify: `tests/agent/test_conversation_loop_bestplan.py`
- Modify: `tests/agent/test_turn_finalizer_iteration_limit_exit.py`
- Modify: `tests/run_agent/test_codex_app_server_integration.py`

- [ ] **Step 1: Pass the graph gate**

Run GitNexus upstream impact for `_run_child_agent`, `run_bestplan`,
`finalize_turn`, the canonical response-usage accounting branch in
`conversation_loop.py`,
the initial/retry max-iteration provider-call helpers in
`agent/chat_completion_helpers.py`,
`agent/codex_runtime.py::_record_codex_app_server_usage`, and the
session-counter initialization/reset paths. Include the max-iteration summary
branch and its retry in the accounting analysis. This task must not edit
provider adapters or global pricing behavior.

- [ ] **Step 2: Write telemetry projector tests**

Add tests for a pure projector equivalent to:

```python
project_child_telemetry(
    {
        "usage_reported": True,
        "usage_coverage": "complete",
        "input_tokens": 1200,
        "output_tokens": 340,
        "total_tokens": 1540,
        "estimated_cost_usd": 0.00421,
        "cost_coverage": "complete",
        "cost_status": "estimated",
        "cost_sources": ["official_docs_snapshot"],
        "cost_provenances": ["usage_derived"],
    },
    latency_ms=1234,
    dispatched=True,
)
```

Assert:

- provider-supplied counters become `usage.status: reported`;
- a genuinely reported all-zero usage object remains `reported` with zeros;
- absent counters become `unavailable` with null values, not zero;
- a non-empty object with no recognized core usage fields remains unavailable;
- one recognized input/output side without the other becomes `partial`, with
  null aggregate token values;
- zero-initialized counters with `usage_reported: false` remain unavailable;
- output records `host_dispatches: 1` for a dispatched child and zero for a
  preflight/construction failure;
- the legacy `api_calls` value is not projected or described as a provider
  request count;
- availability is never inferred from legacy `api_calls` or numeric token
  values;
- cost status accepts only `actual`, `estimated`, `included`, and `unknown`;
- cost source accepts only the existing `CostSource` values;
- cost provenance accepts only `usage_derived`, `provider_reported`,
  or `billing_contract`, with status/provenance combinations validated;
- cost coverage accepts only `complete`, `partial`, or `none`; only complete
  coverage may expose amount/status/source/provenance values;
- partial/none coverage projects a null amount, `unknown`, and empty
  source/provenance arrays rather than a known subtotal;
- usage-derived `estimated` cost becomes unknown/null unless usage coverage is
  complete;
- independently provider-reported `actual` or contract-backed `included` cost
  may survive partial/none usage only with the matching allowlisted
  provenance;
- included zero differs from unknown null;
- non-finite, negative, boolean, or malformed numbers are rejected or mapped to
  unknown according to the spec;
- runtime/messages/errors/URLs/headers and `SENTINEL_SECRET` are absent; and
- output has exact keys.

- [ ] **Step 3: Write real-path usage-provenance RED tests**

In `tests/agent/test_usage_provenance.py`, exercise the normal provider
response accounting and finalization path with:

- a provider response containing a usage object with non-zero counters;
- a provider response containing a real all-zero usage object;
- a non-empty but unrecognized usage object;
- input-only and output-only partial objects;
- a successful provider response with no usage object; and
- a multi-call turn where every successful response reports usage;
- a mixed multi-call turn where only some successful responses report usage;
- a multi-call turn with an earlier unknown-priced response and a later
  estimated response;
- multi-call turns with all estimated, actual-plus-estimated, mixed
  allowlisted sources, included-plus-actual, included-plus-estimated, and
  contract-included-plus-unknown cost records;
- a provider failure/cancellation after dispatch, followed by a known-cost
  response;
- a turn that exhausts the main loop and enters the extra max-iteration
  summary/retry path; and
- a second turn after a prior reported-usage turn.

Assert that the finalized result carries `usage_coverage:
complete|partial|none` plus an explicit boolean `usage_reported`. It is true
only when every model-driving call contributing to the result supplied a
complete recognized canonical input/output pair; a merely non-empty object is
insufficient. A mixed complete/partial/none turn is partial. Until the
max-iteration summary/retry path has explicit usage extraction, entering it
sets an opaque-call flag and forces partial coverage rather than pretending
the legacy main-loop `api_calls` count is complete. The next turn resets
host-owned coverage state before dispatch. Do not infer coverage from
cumulative session token counters.

Also assert a host-owned per-turn cost ledger opens every model-driving
dispatch before the call and closes it exactly once. The finalized result
carries `cost_coverage: complete|partial|none`, a qualified amount only for
complete coverage, sorted unique allowlisted sources/provenances, and an
aggregate status derived from all calls. An earlier unknown, failed,
cancelled, or uninstrumented call cannot be hidden by a later estimated call;
mixed known/unknown produces partial coverage with null amount and unknown
status. All-known mixed actual/estimated calls sum once and become estimated.
All-included stays included; included-plus-actual becomes actual;
included-plus-estimated becomes estimated, and every included record is a
contract-backed zero.
The next turn resets the ledger. Explicitly prove that the pinned legacy
`session_estimated_cost_usd` plus last-written `session_cost_status` and
`session_cost_source` cannot make an incomplete subtotal project as complete.

In `tests/run_agent/test_codex_app_server_integration.py`, exercise the
Codex app-server early-return path with a present non-zero usage dictionary, a
present all-zero usage dictionary, and absent usage. Assert that the terminal
result reports complete/true, complete/true, and none/false respectively. Add
total-only/input-only/unrecognized Codex dictionaries as partial or none per
the exact classifier. A single-agent app-server total may be complete only if
the runtime marks it authoritative for the whole turn; Ultra/multi-agent or
otherwise opaque internal activity is partial even when the outer turn has a
usage object. The helper's separate auxiliary-compaction call cannot leak
coverage into the next terminal turn. Together with the normal-path mixed
test, this proves honest coverage across both accounting surfaces. Its cost
record is complete only when the app-server reports one authoritative
whole-turn amount; otherwise internal multi-agent or auxiliary activity makes
cost partial/unknown even if the outer response has an amount.

- [ ] **Step 4: Write orchestrator telemetry RED tests**

Use a fake monotonic clock and child results to prove:

- every dispatched explorer has latency and telemetry;
- `total_latency_ms` starts after successful preflight/admission and before the
  first concurrent explorer submission, ends at host terminal result, and is
  wall-clock rather than sum/max of child latencies;
- admitted failure, synthesis/reformat, and timeout fixtures end at terminal
  classification, while a never-admitted preflight failure has null total;
- the synthesizer has a `calls` array with one `synthesis` entry and an
  aggregate;
- a compact-body retry appends one `reformat` call in dispatch order;
- synthesis and reformat use distinct fresh child instances so the second
  dispatch cannot return cumulative session tokens or cost;
- non-zero first/second dispatch fixtures aggregate each exactly once;
- success, provider error, and timeout latency are deterministic;
- a non-dispatched credential/runtime/construction failure has null latency;
- attempts remain in scheduled order after out-of-order completion;
- Kimi/Anthropic, custom Chat Completions, and Codex result shapes normalize;
- app-server activity never exposes a fabricated internal request/turn count;
- usage-derived estimated cost becomes unknown under partial/none coverage,
  while independently actual/included fixtures retain their qualified value;
- structured result contains telemetry;
- the finalized host-branch result carries the identical bounded telemetry
  object and exact structured receipt for post-validation success and failure;
- `receipt` and persisted V2 JSON remain byte/schema compatible; and
- visible and streamed output contain no tokens, cost, or receipt marker.

Cover valid first-pass synthesis, successful reformat, invalid reformat,
reformat timeout, and reported/partial/unavailable mixtures.

- [ ] **Step 5: Run the RED tests**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_usage_provenance.py \
  tests/agent/test_bestplan_telemetry.py \
  tests/agent/test_bestplan_orchestrator.py \
  tests/agent/test_conversation_loop_bestplan.py \
  tests/agent/test_turn_finalizer_iteration_limit_exit.py \
  tests/run_agent/test_codex_app_server_integration.py -q
```

Expected: FAIL because usage provenance is absent, `_run_child_agent` still
discards the full result, synthesis retries are not represented separately,
and the host branch drops structured telemetry.

- [ ] **Step 6: Add explicit provider-usage provenance**

Implement a private typed `BestPlanTurnAccountingLedger` in
`agent/bestplan_telemetry.py` and attach it only to BestPlan child agents. It
is initialized in `agent/agent_init.py`, reset at the start of every child
turn, and finalized once. It is separate from the legacy cumulative session
fields. Every model-driving call obtains an opaque ledger token before
dispatch, then closes that exact token with normalized usage and cost or an
explicit `failed|cancelled|unknown` terminal record. Duplicate close,
unmatched close, or an open token at finalization fails coverage closed.

Classify raw usage only after canonical recognition:

- `complete` requires recognized input/prompt and output/completion fields;
  total may be verified or derived, and an all-zero core pair is complete;
- `partial` has at least one recognized core field but not the pair; and
- `none` includes absent, empty, and non-empty unrecognized objects.

Record one per-call cost tuple
`(amount_usd, status, source, provenance, authoritative_scope)`. Validate
finite non-negative amounts, the existing `CostStatus`/`CostSource`
allowlists, and exact provenance rules. `authoritative_scope` is
`dispatch|whole_turn`; the latter is accepted only from an explicitly
supported provider/app-server contract and supersedes, rather than adds to,
dispatch records.

Wrap the normal conversation-loop provider call, Codex runtime call, the
max-iteration summary call, and its empty-summary retry with the ledger. The
deterministic verification fallback opens no token because it makes no model
call. Any future call site lacking a ledger token is marked opaque by a
finalization invariant and cannot report complete coverage.

The two max-iteration provider dispatches live in
`agent/chat_completion_helpers.py`, not `turn_finalizer.py`. Pass the optional
typed ledger into that helper, open/close a distinct token around the initial
call and the retry, and return their normalized accounting with the text.
`turn_finalizer.py` must not infer two calls from one returned string.

Derive usage coverage as complete only when every opened top-level dispatch
closed with a complete canonical pair; mixed complete/partial/none/failed or
opaque coverage is partial when any recognized usage exists, otherwise none.
Only complete usage exposes aggregate counters.

Derive cost coverage independently:

- `complete` when every opened dispatch closes with one qualified amount, or
  one authoritative whole-turn amount covers all activity;
- `partial` when at least one amount is qualified but any dispatch is unknown,
  failed, cancelled, opaque, or uncovered; and
- `none` when no amount is qualified.

Only complete cost coverage exposes the sum, aggregate status, and sorted
unique source/provenance arrays. Aggregate status uses the precedence
`estimated > actual > included`: all-included remains included, any estimated
record makes the complete aggregate estimated, and otherwise any actual record
makes it actual. Included records require a contract-backed zero amount.
Partial or none exposes null amount, unknown status, and empty arrays. An earlier unknown
can never be overwritten by a later known call. Return both usage fields and
the cost aggregate from this ledger; cumulative session token/cost fields,
legacy `api_calls`, and last-written `session_cost_status/source` do not
participate.

Do not infer completeness from numeric values, object truthiness, model
family, or cost status.

The Codex app-server path bypasses `finalize_turn`. Extend
`_record_codex_app_server_usage` so its returned allowlisted usage result
contains explicit `usage_coverage` and whether the usage total is authoritative
for the complete turn. The terminal caller derives and returns coverage plus
`usage_reported`. Absent/unrecognized usage is none/false, partial recognized
fields are partial/false, and a present all-zero recognized core pair is
complete/true only for a non-opaque single-agent turn. Ultra/multi-agent stays
partial unless the app-server protocol later supplies authoritative aggregate
coverage. The auxiliary compaction caller may reuse accounting but cannot
mutate terminal-turn coverage. Apply the same rule to cost: only an
authoritative whole-turn provider amount can close cost coverage for opaque
internal activity; otherwise any visible subtotal is partial and projects as
unknown.

- [ ] **Step 7: Implement the allowlisted projector**

Implement immutable/bounded output equivalent to:

```python
{
    "latency_ms": int | None,
    "host_dispatches": 0 | 1,
    "usage": {
        "status": "reported" | "partial" | "unavailable",
        "input_tokens": int | None,
        "output_tokens": int | None,
        "total_tokens": int | None,
    },
    "cost": {
        "amount_usd": float | None,
        "coverage": "complete" | "partial" | "none",
        "status": "actual" | "estimated" | "included" | "unknown",
        "sources": list[str],
        "provenances": list[
            "usage_derived" | "provider_reported" | "billing_contract"
        ],
    },
}
```

Only `reported` carries token totals. `partial` and `unavailable` use null
token values and remain distinct for coverage reporting. A usage-derived
estimated cost requires `reported`; partial/unavailable forces unknown/null.
Only independently provider-reported actual or contract-backed included cost
may remain known without complete token usage. Only complete cost coverage
carries an amount and non-empty source/provenance arrays; partial/none carries
null/unknown/empty values. Do not serialize the raw child result or its legacy
`api_calls`.

- [ ] **Step 8: Retain the child result at the existing seam**

Change `_run_child_agent` to return final text plus the allowlisted telemetry
projection. Measure elapsed time with `time.monotonic()` around the one child
turn.

For pending futures classified as timeout, compute host latency at terminal
classification and mark usage/cost unavailable if the result never became
available. Do not wait beyond existing deadlines to collect counters.

- [ ] **Step 9: Represent every synthesis dispatch**

Store synthesis telemetry as:

```python
{
    "calls": [
        {"kind": "synthesis", **call_telemetry},
        # Optional second entry:
        {"kind": "reformat", **call_telemetry},
    ],
    "aggregate": {...},
}
```

Construct a fresh synthesizer child for `synthesis` and, only if needed, a
second fresh child for `reformat`, using the same named runtime and complete
bounded prompt context. Stop each independently. Never reuse session counters
or compute per-dispatch telemetry from a cumulative child.

Aggregate stage latency from first dispatch to terminal classification and sum
host dispatches exactly. Aggregate token buckets only when every dispatch has
complete compatible coverage; otherwise use null with partial/unavailable
status. Sum cost only when every fresh dispatch has a known amount with valid
provenance. Use precedence `estimated > actual > included`: all-included is
included, any estimated call makes the complete aggregate estimated, otherwise
any actual call makes it actual; any unknown call makes it unknown. Cost coverage is complete only when every
call is complete, partial when at least one is complete and another is not,
and none when none is complete. Partial/none aggregate values are
null/unknown/empty. Preserve sorted unique allowlisted sources and provenances
only for a complete aggregate.

- [ ] **Step 10: Expose telemetry through both host boundaries**

Add a bounded `telemetry` object to `run_bestplan()` results:

```python
{
    "attempts": [...],
    "synthesizer": {
        "calls": [...],
        "aggregate": {...},
    },
    "total_latency_ms": ...,
}
```

Start the total monotonic timer immediately after successful runtime/evaluation
admission and before submitting any explorer; stop it only when constructing
the terminal host result. Do not sum concurrent child latencies. Use terminal
classification time for admitted timeout/failure; leave it null only when
preflight never admitted the run. This field, not child/stage latency, is the
evaluation p95 gate input.

After the BestPlan host branch finalizes the turn, attach the exact same
bounded telemetry object and marker-wrapped structured receipt to the returned
result for every post-validation terminal outcome. Do not reconstruct either
from receipt metadata or visible response text.

Do not add any telemetry key to receipt V2, persisted assistant content,
streamed text, or `final_response`.

- [ ] **Step 11: Run the GREEN tests**

Run the command from Step 5.

Expected: PASS.

- [ ] **Step 12: Stage, inspect, and commit**

Run:

```bash
git add agent/bestplan_telemetry.py \
  agent/bestplan_orchestrator.py \
  agent/conversation_loop.py \
  agent/chat_completion_helpers.py \
  agent/codex_runtime.py \
  agent/agent_init.py \
  agent/turn_finalizer.py \
  run_agent.py \
  tests/agent/test_usage_provenance.py \
  tests/agent/test_bestplan_telemetry.py \
  tests/agent/test_bestplan_orchestrator.py \
  tests/agent/test_conversation_loop_bestplan.py \
  tests/agent/test_turn_finalizer_iteration_limit_exit.py \
  tests/run_agent/test_codex_app_server_integration.py
git diff --cached
```

Run GitNexus staged `detect_changes`, then commit:

```bash
git commit -m "feat(bestplan): capture per-model runtime telemetry"
```

### Task 5: Add strict experiment switches and Candidate V2

**Files:**

- Create: `agent/bestplan_candidate.py`
- Create: `tests/agent/test_bestplan_candidate.py`
- Modify: `agent/bestplan_orchestrator.py`
- Modify: `hermes_cli/config.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`
- Modify: `tests/hermes_cli/test_bestplan_cli.py`

- [ ] **Step 1: Pass the graph gate**

Run GitNexus upstream impact for `validate_runtime`, `validate_candidate`,
`_candidate_from_text`, and `run_bestplan`.

- [ ] **Step 2: Write config RED tests**

Add tests proving:

- `candidate_contract` defaults to `v1`;
- `lens_contract` defaults to `current`;
- explicit `v1|evidence_v2` and `current|operational` values normalize;
- unknown, empty, non-string, and extra nested values fail before dispatch;
- canonical and legacy lane adapters preserve the same experiment settings;
- any non-current experiment (`evidence_v2` or `operational`) fails preflight
  when a scheduled explorer or synthesizer uses `codex_app_server`;
- any non-current experiment fails preflight when a resolved
  `codex_responses` transport has `is_xai_responses: true`, because the pinned
  transport can replace Hermes `web_search` with provider-native live search;
- an ordinary non-xAI `codex_responses` runtime is eligible only when resolved
  transport capabilities prove client-side tool preservation;
- current V1 compact/telemetry behavior remains available for app-server
  runtimes and does not claim scoped native tools; and
- model pool order, count, synthesizer, and K3 trust behavior are unchanged.

- [ ] **Step 3: Write strict Candidate V2 RED tests**

Test:

- exact marker at byte zero;
- one complete JSON object and no trailing text;
- exact top-level/evidence key sets;
- all list and string bounds;
- valid and dangling claim pointers;
- `task|file|web` kind enum;
- exact kind-specific locator objects:
  - workspace-relative `file_line` with bounded start/end lines;
  - host-issued visible `task_fact` ID; and
  - exact canonical `web_source` URL;
- absolute/traversing file paths, unknown task fact IDs, search-query-only web
  evidence, unfetched URLs, mixed-kind locator shapes, and extra locator keys
  fail closed;
- `direct|indirect|counterevidence` support enum;
- zero evidence accepted only when `unknowns` is non-empty;
- controls and oversized raw packets rejected before JSON decode;
- no confidence or model-identity field accepted;
- checked-in characterization fixtures preserve every V1 packet shape
  accepted by the pinned base, including its current permissive surrounding
  prose and truthy shared fields;
- those same V1 fixtures succeed only under `candidate_contract: v1`; and
- V1 is `candidate_invalid` under `evidence_v2`.

Before refactoring, run the V1 characterization tests against the pinned
implementation and record the exact pass set. Use small factories rather than
repeating large packets.

Add separate evaluation-canonicalizer tests proving that it:

- emits only bounded `summary/steps/risks/verification` fields shared by V1
  and V2;
- assigns deterministic candidate-local claim/finding IDs;
- never changes whether the production V1 packet counts toward quorum;
- returns a bounded `unscoreable` reason for permissive V1 shapes that cannot
  be safely projected; and
- handles V2 assumptions, unknowns, and evidence only in separate V2-specific
  projections.

- [ ] **Step 4: Run the RED tests**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_bestplan_candidate.py \
  tests/agent/test_bestplan_orchestrator.py \
  tests/hermes_cli/test_bestplan_cli.py -q
```

Expected: FAIL because the experiment keys and Candidate V2 parser do not
exist.

- [ ] **Step 5: Implement strict parsing and validation**

In `agent/bestplan_candidate.py`, implement functions equivalent to:

```python
def parse_candidate(text: str, *, contract: str) -> dict[str, object]: ...

def validate_candidate_v1(candidate: object) -> dict[str, object]: ...

def validate_candidate_v2(candidate: object) -> dict[str, object]: ...

def canonicalize_candidate_for_evaluation(
    candidate: object, *, contract: str, attempt_index: int
) -> dict[str, object]: ...
```

`validate_candidate_v1` must preserve the pinned production behavior exactly;
do not tighten it to make evaluation easier. The separate evaluation
canonicalizer is fail-closed and may mark a production-valid V1 candidate
unscoreable without changing quorum, synthesis, visible output, or receipts.
Do not loosen V2 when a provider returns Markdown or surrounding prose.

- [ ] **Step 6: Integrate experiment settings**

In `validate_runtime`:

- allow the two exact top-level keys;
- normalize strict enum values; and
- return them in canonical runtime state;
- resolve the scheduled runtime modes before dispatch; and
- reject an experimental contract if any scheduled explorer or synthesizer
  uses `codex_app_server`, with a fixed non-secret reason. Do not silently
  rewrite that entry to another API mode;
- inspect resolved transport parameters, not only the generic mode label, and
  reject `is_xai_responses: true` for every experimental/evaluation run with a
  fixed non-secret reason. Do not rely on the registry-time tool list: the
  pinned xAI Responses transport rewrites `web_search` after construction.

In explorer prompt construction, request the contract selected for that run.
Quorum still counts valid candidate packets only.

- [ ] **Step 7: Run the GREEN tests**

Run the command from Step 4.

Expected: PASS.

- [ ] **Step 8: Stage, inspect, and commit**

Run:

```bash
git add agent/bestplan_candidate.py \
  agent/bestplan_orchestrator.py \
  hermes_cli/config.py \
  tests/agent/test_bestplan_candidate.py \
  tests/agent/test_bestplan_orchestrator.py \
  tests/hermes_cli/test_bestplan_cli.py
git diff --cached
```

Run GitNexus staged `detect_changes`, then commit:

```bash
git commit -m "feat(bestplan): add grounded candidate experiment"
```

### Task 6: Admit evidence locators and anonymize synthesis packets

**Files:**

- Modify: `agent/bestplan_candidate.py`
- Modify: `agent/bestplan_orchestrator.py`
- Modify: `agent/conversation_loop.py`
- Modify: `agent/agent_init.py`
- Modify: `run_agent.py`
- Create: `agent/bestplan_inspection.py`
- Modify: `model_tools.py`
- Modify: `tools/file_tools.py`
- Modify: `tools/web_tools.py`
- Modify: `tests/test_model_tools.py`
- Modify: `tests/agent/test_bestplan_candidate.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`
- Create: `tests/tools/test_bestplan_read_scope.py`
- Create: `tests/tools/test_bestplan_evaluation_io.py`

- [ ] **Step 1: Pass the graph gate**

Run GitNexus upstream impact for the new candidate parser, `_run_child_agent`,
`run_bestplan`, `read_file_tool`, every `search_files` entry point, and the
`web_search`/`web_extract` registry handlers,
`model_tools.get_tool_definitions`, `check_web_search_available`,
`check_web_extract_available`, `web_capability_fingerprint`,
`model_tools.handle_function_call`, its middleware/plugin-hook seams,
`AIAgent.__init__`, `init_agent`, and the parent task/workspace-context seam in
`conversation_loop.py`. Inspect the registry override/dynamic-schema path even
though evaluation construction will bypass it. Reuse existing
file-safety helpers where their contracts fit; run impact before editing any
reused helper. Warn before proceeding if any result is HIGH or CRITICAL.

Keep the registry, immutable `BestPlanReadScope`, typed
`BestPlanEvaluationIO`, and canonical provenance helpers in
`agent/bestplan_inspection.py`. File/web handlers import only this narrow
module; they must not import the orchestrator or evaluator script.

- [ ] **Step 2: Write enforced read-scope RED tests**

Define an immutable `BestPlanReadScope` and scoped registry tests proving:

- the parent effective task ID and authoritative workspace root are resolved
  once before child construction; missing/ambiguous roots fail before dispatch;
- each concurrent explorer/synthesizer receives a unique derived task ID
  registered to that exact parent root, and `_run_child_agent` passes it to
  `run_conversation(..., task_id=...)`;
- relative and absolute in-root reads/searches succeed;
- `..`, absolute outside-root paths, symlink escapes, sibling worktrees, and
  search results outside the root are blocked after realpath resolution;
- two concurrent BestPlan runs rooted at different worktrees cannot see each
  other's files; and
- derived task IDs have a reserved, random, non-reusable BestPlan prefix;
- any prefixed task ID without an active registry entry fails closed instead
  of falling back to the ordinary default root;
- timeout first deactivates the scope, and a quarantined worker's late
  read/search after failed teardown is denied;
- active registry entries are removed after completion, while the reserved
  prefix rule preserves the fail-closed tombstone semantics without an
  unbounded per-task tombstone set; and
- ordinary non-BestPlan task IDs keep their current file-tool behavior.

In `tests/tools/test_bestplan_evaluation_io.py`, define a private typed
`BestPlanEvaluationIO` and prove:

- an exact workspace manifest is verified before dispatch and every arm uses
  the same content-addressed read-only root;
- evaluation-prefixed `web_search` and `web_extract` calls receive the
  matching frozen result/body for exact registered queries and canonical URLs;
- with no live web provider configured, full child tool-definition assembly
  still exposes frozen `web_search`/`web_extract` for a registered evaluation
  task ID;
- the evaluation adapter is registered before tool definitions are built, and
  `web_capability_fingerprint` includes its generation/corpus digest so
  memoization adds a new adapter and removes an expired one without leaking
  capability to an ordinary task;
- `_run_child_agent` derives/registers the child task ID before construction,
  `_build_child_agent(task_id=...)` passes it through `AIAgent.__init__`,
  `init_agent`, and `get_tool_definitions(task_id=...)`, and
  `run_conversation` later
  validates the same immutable ID;
- two agents constructed concurrently with different evaluation IDs receive
  only their own frozen capabilities, while an ordinary agent receives the
  unchanged live/global definitions;
- sequential evaluation-A, ordinary-task, evaluation-B, and expired-A
  construction proves the mutable registry/its callable-only TTL cache is not
  consulted for evaluation definitions and cannot leak results across IDs;
- malicious registry handler/schema/check replacements and dynamic schema
  overrides are installed, but evaluation construction exposes only exact
  canonical built-in schemas and invokes none of them; ordinary construction
  still observes the authorized overrides;
- malicious global request-rewrite, pre, post, and result-transform hooks are
  installed, but none is invoked for an evaluation-prefixed task; the exact
  frozen path/query/result bytes survive unchanged and no hook side effect is
  recorded;
- an evaluation request for any tool outside the closed frozen
  file/search/web allowlist fails before global middleware or plugin dispatch;
- unknown query/URL, browser access, missing/expired adapter, live backend
  fallback, and network access fail closed;
- case A cannot use case B's snapshot or corpus;
- current checkout mutations after snapshot creation cannot change results;
- `codex_app_server` is rejected before any experimental/evaluation child is
  constructed; and
- resolved xAI Responses is rejected before child construction even when its
  generic mode is `codex_responses`; a transport fixture proves the
  provider-native `web_search` rewrite is never reached.

- [ ] **Step 3: Write evidence-admission RED tests**

Add tests proving:

- a valid structured in-root `file_line` locator becomes `resolved`;
- the host computes a non-null opaque admission provenance digest;
- missing, out-of-range, directory, symlink-escape, traversal, and outside-root
  locators become `unresolved`;
- task evidence becomes `observed` only for a host-issued visible fact ID;
- web evidence becomes `observed` only when its exact canonical URL appears in
  a matching successful fetched/opened source result; a search result or query
  alone stays unresolved;
- hidden gold facts are never issued to the explorer;
- the isolated evaluator gives every arm the same frozen fact/URL source
  adapter, and a URL/content change fails exact mapping rather than fuzzy
  matching;
- file evidence is served only from the case's verified immutable workspace
  snapshot, never the evaluator cwd or a mutable checkout;
- evaluation file evidence maps to a safe source only when its normalized
  path and whole cited interval match or are an unambiguous subset of one
  owner-approved snapshot-backed range;
- a superset, cross-boundary interval, unapproved path, changed byte range, or
  overlap with multiple approved ranges remains unmapped and fails closed for
  evidence scoring;
- distinct issued task facts and successful web observations produce distinct
  stable provenance IDs, while the same canonical fact/observation is stable;
- file provenance comes from the admission-time scoped bounded reread, task
  provenance from the issued visible-fact record, and only web provenance
  requires exact call-ID/result matching; the volatile web call ID itself is
  excluded from the stable hash;
- null, malformed, or conflicting provenance for resolved/observed evidence
  fails closed;
- an orphaned or failed tool result does not count as observation;
- locator status does not claim semantic verification;
- raw source bytes, notes, paths, URLs, messages, and sentinel secrets are
  absent from telemetry and receipts; and
- admission is bounded in count and bytes.

- [ ] **Step 4: Write private evaluation-capture RED tests**

Define a private typed `_EvaluationArtifactSink` accepted only as a
keyword-only in-process dependency of `run_bestplan`. Prove:

- default production `v1/current` with no experimental dependency preserves
  its exact legacy synthesis packet;
- for both the legacy default and common experimental envelope, attaching
  versus omitting the sink leaves the packet selected by that mode, production
  results, visible output, receipt V2, telemetry, and quorum unchanged;
- arbitrary dictionaries/callables cannot masquerade as the typed sink;
- each scoreable candidate reaches the sink before `visible_attempts()` drops
  the private `_candidate`;
- the sink receives only attempt index, schema, bounded common
  `summary/steps/risks/verification` candidate claims/findings, V2-specific
  assumption/unknown aggregates, and admitted evidence metadata;
- each V2 evidence item retains only its candidate-local claim link, support
  class, locator status, evidence kind, and host-resolved safe
  `case_source_id`;
- a production-valid but unsafe V1 projection records only a bounded
  `unscoreable` code;
- every terminal path emits one exact identity-free execution projection with
  `scheduled_attempts`, `terminal_attempts`, `valid_candidates`,
  `candidate_invalid`, `timed_out`, `quorum_required`, `quorum_met`,
  `synthesis_started`, `synthesis_succeeded`, and normal-host
  `interrupted: false`;
- successful, candidate-invalid, quorum-failed, synthesis-failed, and timeout
  runs produce correct counts/booleans even when no candidate artifact exists;
- no configured or resolved model/provider/explorer identity, raw candidate,
  locator, URL, evidence note, source bytes, message, tool trace, provider
  error, endpoint, credential, or `SENTINEL_SECRET` reaches the sink; and
- sink failure is isolated to the evaluator and cannot mutate or persist a
  partial production receipt.

- [ ] **Step 5: Write anonymous-packet RED tests**

Prove that the synthesizer packet contains:

```json
{
  "attempt": {
    "index": 0,
    "lens": "evidence-first"
  },
  "candidate": {},
  "admission": []
}
```

for every evaluation arm and non-current contract, and does not contain
configured/resolved provider, model, explorer name, credential, endpoint, or
raw tool trace. All four 2-by-2 arms use this same envelope shape. Also prove
that ordinary non-evaluation `v1/current` keeps the pinned legacy packet; do
not assert that legacy and experimental packets are byte-identical.

Also prove that final visible model identity still comes from the host ledger.

- [ ] **Step 6: Run the RED tests**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_bestplan_candidate.py \
  tests/agent/test_bestplan_orchestrator.py \
  tests/test_model_tools.py \
  tests/tools/test_bestplan_read_scope.py \
  tests/tools/test_bestplan_evaluation_io.py -q
```

Expected: FAIL because the child read scope, deterministic evaluation adapter,
evidence admission, private evaluation sink, and anonymous envelopes do not
yet exist.

- [ ] **Step 7: Implement the enforced child read scope**

Resolve the parent effective task ID and authoritative workspace root in the
host BestPlan branch. Register one immutable `BestPlanReadScope`, derive
unique child task IDs, and pass them to every child conversation. Add
BestPlan-only scope checks to `read_file` and all `search_files` surfaces after
path normalization/realpath, including every returned search result.

Use a reserved random BestPlan task-ID prefix that cannot collide with
ordinary tasks. Missing/expired prefixed IDs always deny. Admission receives
the same scope object. On timeout, deactivate before teardown/quarantine; a
late worker remains denied even when it survives the host deadline. Remove
active entries in `finally`, relying on the reserved-prefix deny rule rather
than default-root fallback. Do not turn the existing workspace-divergence
warning into a global read prohibition for ordinary Hermes turns.

- [ ] **Step 8: Implement bounded inspection extraction**

At the child-result seam, extract only successful tool-call identities and
bounded locator metadata needed for admission. Match tool results to calls by
exact call ID. Do not retain raw messages after projection.

Build a bounded visible-task-fact registry before prompting explorers. In
evaluation mode, require a typed `BestPlanEvaluationIO` containing the
verified case snapshot root, visible facts, and frozen search/fetch corpus.
Pass its derived task ID through the real tool dispatcher. Update
`tools/web_tools.py` handlers to receive `task_id`, consult the private
registry for evaluation-prefixed IDs, and return only exact frozen responses.
Unknown or missing evaluation entries fail closed without invoking a live web
provider. A search query is not evidence until a registered source URL is
fetched/opened successfully.

Update `model_tools.py` so evaluation-aware availability checks receive the
derived task ID and consult the typed adapter before global provider checks.
Generate and register the derived child ID before `_build_child_agent`.
Add an optional keyword-only `task_id` path through `_build_child_agent`,
`AIAgent.__init__`, `init_agent`, and
`model_tools.get_tool_definitions`. Store the immutable construction ID on the
child and require
`run_conversation` to receive the same value. For evaluation IDs,
`get_tool_definitions` bypasses `tools.registry` and its callable-only TTL
cache, returning exact host-owned schemas for the frozen file/search/web
allowlist. It must not invoke registry handler/schema/check callbacks or
dynamic schema overrides. Include task ID plus the adapter's immutable
generation/corpus digest in any evaluation-only cache key; removal invalidates
the exact entries. Ordinary construction keeps the existing registry and
cache behavior.

Add an evaluation-prefixed branch at the start of
`model_tools.handle_function_call`. It validates the active typed adapter and
dispatches only the closed frozen file/search/web handlers directly. It
bypasses global tool-request middleware and every plugin pre/post/transform
hook; unknown tools or missing/expired adapters fail closed. Ordinary task IDs
continue through the existing hook pipeline unchanged.

The adapter is keyword-only and in-process, not config/CLI/environment. Reject
`codex_app_server` and resolved xAI Responses for every non-current experiment
because they can bypass these Hermes handlers; do not claim prompt
instructions or global approvals contain provider-native tools. Keep generic
non-xAI `codex_responses` eligible only after resolved capability validation.

- [ ] **Step 9: Implement locator admission**

Validate exact kind-specific locator objects. Normalize relative file paths
against `BestPlanReadScope`, read only the bounded cited range, and compute the
digest in the host. Match task IDs only to the visible-fact registry and web
URLs only to exact successful fetched/opened results. Never execute a
candidate-provided command or perform a second unrestricted web fetch.

Derive `admission_provenance_sha256` from the authoritative kind-specific
surface. File provenance binds the admission-time scoped reread bytes and
canonical bounded locator metadata. Task provenance binds the exact issued
fact record without a tool call. Only web provenance requires exact
call/result matching and binds the canonical fetched URL plus matched
successful result bytes. Unresolved evidence has null provenance. Strip the
digest before synthesis; do not log or persist raw metadata.

In evaluation mode, resolve provenance while the private raw locator is still
available. Exact task/web mappings come from the frozen corpus. File mappings
use owner-approved `(path, line_start, line_end, source_bytes_sha256,
case_source_id)` records: exact and uniquely contained subranges map; supersets,
cross-boundary or ambiguous ranges do not. Pass only the safe
`case_source_id` to the evaluation sink. Never synthesize a map post hoc or
use semantic/fuzzy matching.

- [ ] **Step 10: Implement the private evaluation sink seam**

Add `_EvaluationArtifactSink` beside the evaluation canonicalizer in
`agent/bestplan_candidate.py`. `run_bestplan` accepts it only as a
keyword-only in-process object; it is not reachable from config, CLI,
environment, provider entries, or receipts.

Immediately after structural validation and evidence admission, emit the
bounded canonical evaluation projection before `visible_attempts()` removes
private candidate state. Keep candidate-local claims and evidence links
separate from the eventual synthesized plan. The sink may receive the
evidence kind, locator status, and already resolved safe `case_source_id`;
the host uses provenance plus the owner-only map while the raw locator is
available. It receives no admission digest, raw locator, call ID, query, tool
result, or source content. Sink absence is the production path and must remain
byte-for-byte compatible.

Before every terminal return, call the typed sink once with the exact bounded
execution projection derived directly from host attempt/quorum/synthesis
state. Do not reconstruct it from receipt text or infer invalid candidates
from missing candidate projections. Sink lifecycle state must not enter the
ordinary outcome, receipt V2, telemetry, or visible text when no evaluator is
attached.

- [ ] **Step 11: Build anonymous synthesis envelopes**

Select packet mode independently of the sink. Ordinary production
`v1/current` with no experimental dependency uses the exact pinned packet.
Every evaluation arm and any non-current contract uses the common bounded
anonymous envelope, with V1 admission empty where applicable. Keep candidate
ordering by invocation index. Tell the experimental synthesizer:

- attempt/lens/admission metadata is host-owned;
- candidate claims and source interpretation remain untrusted;
- resolved/observed support outranks unresolved assertions;
- counterevidence and supported minority findings must be preserved; and
- candidate count is not a vote.

- [ ] **Step 12: Run the GREEN tests**

Run the command from Step 6.

Expected: PASS.

- [ ] **Step 13: Stage, inspect, and commit**

Run:

```bash
git add agent/bestplan_candidate.py \
  agent/bestplan_inspection.py \
  agent/bestplan_orchestrator.py \
  agent/conversation_loop.py \
  agent/agent_init.py \
  run_agent.py \
  model_tools.py \
  tools/file_tools.py \
  tools/web_tools.py \
  tests/test_model_tools.py \
  tests/agent/test_bestplan_candidate.py \
  tests/agent/test_bestplan_orchestrator.py \
  tests/tools/test_bestplan_read_scope.py \
  tests/tools/test_bestplan_evaluation_io.py
git diff --cached
```

Run GitNexus staged `detect_changes`, then commit:

```bash
git commit -m "feat(bestplan): admit evidence without identity bias"
```

### Task 7: Turn existing strategies into operational lenses

**Files:**

- Modify: `agent/bestplan_candidate.py`
- Modify: `agent/bestplan_orchestrator.py`
- Modify: `tests/agent/test_bestplan_candidate.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`

- [ ] **Step 1: Pass the graph gate**

Run GitNexus upstream impact for `run_bestplan` and the new lens prompt helper.

- [ ] **Step 2: Write operational-lens RED tests**

Add one exact behavior test per existing strategy:

- `evidence-first` requests facts, locators, assumptions, and freshness;
- `counterfactual` requests alternatives and falsifiers;
- `failure-first` requests severity, trigger, blast radius, containment, and
  rollback hazards;
- `verification-first` requests executable checks, expected signals, negative
  tests, and rollback proof; and
- `scope-first` requests contracts, dependencies, sequencing, and non-goals.

Prove:

- `lens_contract: current` preserves the existing short prompt;
- `operational` uses host-owned text;
- no arbitrary config prompt is accepted;
- lens assignment cycles independently of model-array cycling;
- reordering or replacing explorer models does not bind a lens to a model;
- tools, count, quorum, named synthesis, and K3 guards are unchanged; and
- receipt V2's existing `strategy` field remains valid and exact.

Also test one internal evaluation-only schedule type:

- it is not accepted from YAML, CLI, environment, or arbitrary dictionaries;
- it contains exactly five closed host-owned lens instructions;
- the full schedule reaches all five operational lenses at count five;
- it accepts only a validated host-created permutation row containing each
  closed lens exactly once, never a model ID or arbitrary prompt;
- each of five ablation schedules replaces exactly one checklist with the
  corresponding current neutral strategy text while keeping the same strategy
  name and permutation position; and
- model order, attempt count, limits, tools, quorum, and synthesizer are byte-
  for-byte identical across the six schedules.

- [ ] **Step 3: Run the RED tests**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_bestplan_candidate.py \
  tests/agent/test_bestplan_orchestrator.py -q
```

Expected: FAIL because operational checklists do not exist.

- [ ] **Step 4: Implement the host-owned lens map**

Use a closed immutable mapping from the five existing strategy names to short,
bounded operational checklists. Do not add per-model `role`, an arbitrary
prompt field, confidence, weighted quorum, or a second role registry.

Add a private typed `_EvaluationLensSchedule` accepted only as a keyword-only
in-process dependency of `run_bestplan`. Its constructor accepts closed lens
IDs plus `operational|neutral` mode, validates exactly one permutation of the
five entries, and is
not reachable from runtime config, the CLI, environment variables, receipts,
or provider/model entries. Production calls pass no schedule and remain
unchanged. This narrow seam exists only so the isolated harness can run
matched leave-one-lens-out experiments without monkeypatching or adding a
per-model role surface; remove it if lenses are not promoted.

- [ ] **Step 5: Integrate the selected lens contract**

Build explorer prompts from:

1. the common read-only task contract;
2. the selected candidate contract; and
3. the scheduled host-owned strategy/lens.

Do not reveal other candidate outputs during exploration.

When the private evaluation schedule is supplied, use only its closed
host-owned lens/mode pairs. It cannot alter model assignment, count, tools,
budgets, quorum, or synthesis.

- [ ] **Step 6: Run the GREEN tests**

Run the command from Step 3.

Expected: PASS.

- [ ] **Step 7: Stage, inspect, and commit**

Run:

```bash
git add agent/bestplan_candidate.py \
  agent/bestplan_orchestrator.py \
  tests/agent/test_bestplan_candidate.py \
  tests/agent/test_bestplan_orchestrator.py
git diff --cached
```

Run GitNexus staged `detect_changes`, then commit:

```bash
git commit -m "feat(bestplan): test operational explorer lenses"
```

### Task 8: Build the isolated 2-by-2 and lens-ablation harness

**Files:**

- Create: `scripts/evaluate_bestplan.py`
- Create: `scripts/bestplan_eval_checks.py`
- Create: `tests/scripts/test_evaluate_bestplan.py`
- Create: `tests/fixtures/bestplan_eval_cases.example.jsonl`
- Create: `tests/fixtures/bestplan_eval_results.example.jsonl`
- Create: `tests/fixtures/bestplan_eval_phase_a_bundle.example.jsonl`
- Create: `tests/fixtures/bestplan_eval_phase_a_judgments.example.jsonl`
- Create: `tests/fixtures/bestplan_eval_phase_a_consensus.example.jsonl`
- Create: `tests/fixtures/bestplan_eval_phase_b_bundle.example.jsonl`
- Create: `tests/fixtures/bestplan_eval_phase_b_judgments.example.jsonl`
- Create: `tests/fixtures/bestplan_eval_phase_b_consensus.example.jsonl`
- Create: `tests/fixtures/bestplan_eval_positive_summary.example.json`
- Create: `tests/fixtures/bestplan_eval_runtime.example.yaml`
- Create: `tests/fixtures/bestplan_eval_sampling_frame.example.json`
- Create: `tests/fixtures/bestplan_eval_sampling_frame.example.sha256`
- Create: `tests/fixtures/bestplan_eval_design.example.json`
- Create: `tests/fixtures/bestplan_eval_design.example.sha256`
- Create: `tests/fixtures/bestplan_eval_cost_bounds.example.json`
- Create: `tests/fixtures/bestplan_eval_cost_bounds.example.sha256`
- Create: `tests/fixtures/bestplan_eval_check_attestation.example.json`
- Create: `tests/fixtures/bestplan_eval_source_input.example.json`
- Create: `tests/fixtures/bestplan_eval_source_map.example.json`
- Create: `tests/fixtures/bestplan_eval_source_corpus.example.json`
- Create: `tests/fixtures/bestplan_eval_workspaces/synthetic-001/`
- Modify: `agent/bestplan_orchestrator.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`

- [ ] **Step 1: Pass the graph gate**

Run GitNexus impact for `run_bestplan`, `_build_child_agent`,
`_EvaluationLensSchedule`, and the new evaluation host-dispatch budget helper.
Also cover the typed `BestPlanEvaluationIO` registry from Task 6. The
2-by-2 harness calls normal `run_bestplan` through dependency injection. The
ablation phase may use only the closed in-process schedule dependency created
in Task 7; it must not add a configuration, CLI, environment, arbitrary
prompt, or per-model role surface.

- [ ] **Step 2: Define a redacted case schema**

The synthetic fixture demonstrates:

```json
{
  "case_id": "synthetic-001",
  "case_family_id": "family-synthetic-001",
  "category": "debugging",
  "difficulty": "easy",
  "observation_cutoff": "2026-07-01T12:00:00Z",
  "source_provenance": {
    "kind": "git_commit",
    "id": "synthetic-40-hex-commit"
  },
  "task": "Redacted planning task",
  "workspace_snapshot_id": "workspace-synthetic-001",
  "workspace_manifest_sha256": "sha256-of-canonical-manifest",
  "visible_task_facts": [
    {
      "task_fact_id": "task-fact-001",
      "text": "The synthesizer must name a configured explorer."
    }
  ],
  "critical_facts": ["fact-id"],
  "constraints": ["must-preserve-id"],
  "forbidden_actions": ["no-restart"],
  "acceptance_checks": ["observable-check-id"],
  "evidence_opportunities": {
    "direct": true,
    "indirect": false,
    "counterevidence": false
  },
  "case_sources": [
    {
      "case_source_id": "source-001",
      "kind": "redacted_text",
      "redacted_support_text": "The configured synthesizer must name an explorer.",
      "safe_text_sha256": "sha256-of-exact-redacted-text"
    },
    {
      "case_source_id": "source-002",
      "kind": "executable",
      "check_id": "observable-check-id"
    }
  ]
}
```

Reject credentials, obvious token patterns, absolute private home paths, raw
conversation exports, duplicate IDs, and unbounded fields before any run.
`difficulty` is exactly `easy|standard|hard`. `case_family_id` is a blinded
pre-output incident/retry lineage; corpus preparation selects exactly one
representative case per family for controlling pilot/confirmation, and no
family crosses those splits. `observation_cutoff`, exact source commit/archive
provenance, difficulty, family, and snapshot/source digests are frozen before
condition IDs or output exist. Reject post-cutoff files/web data unless the
manifest proves they were part of the original task, and keep accepted
resolution/gold outcomes post-cutoff and scorer-only. A post-resolution
leakage sentinel must fail before dispatch. Missing/unknown difficulty,
family, cutoff, or provenance fails before dispatch. `workspace_snapshot_id` resolves only
through the owner-only corpus to a redacted content-addressed archive/tree.
Verify an exact canonical manifest of relative paths, sizes, and SHA-256
values, reject symlinks/devices/traversal, extract to a fresh read-only root,
and bind every arm for that case to the same digest. The evaluator cwd and a
mutable live checkout are never case evidence.

`case_sources` is exact-key validated and bounded. Every record supplies
exactly one judgeable surface: redacted text whose digest verifies, or a
closed executable check ID.
`evidence_opportunities` is frozen from the safe case rubric before outputs;
it prevents a missing natural indirect/counterevidence class from becoming a
quota or incentive to fabricate a citation.

Define a separate owner-only source-map schema. Task and web records map one
exact `(kind, admission_provenance_sha256)` pair to one `case_source_id`.
File records instead declare
`(case_source_id, workspace_snapshot_id, path, line_start, line_end,
source_bytes_sha256)`. The evaluator validates all IDs/digests, creates or
copies a live map with mode `0600`, and never exposes it to scorers. At
admission, an exact file range or an unambiguous subrange maps to the approved
safe ID; a superset, cross-boundary range, ambiguous overlap, changed bytes,
or unknown path does not. Reject null/malformed task/web provenance, one key
or file interval mapped to conflicting safe sources, missing/duplicate
mappings, unknown IDs, safe-text digest mismatches, query/call-ID fields, and
evidence that resolves to neither safe redacted text nor an executable check.

Define a second owner-only frozen source-corpus schema for the evaluation
adapter. It contains only the issued visible task facts, approved canonical
web URLs with deterministic search/fetch content, and verified workspace
snapshot/manifest records plus approved file-source range definitions. It is
mode `0600`, exact-key validated, tied to one `evaluation_id`, and never
supplied to scorers or production config. Raw URLs/content, paths, ranges, and
snapshot locations may exist only in this protected corpus; they are absent
from structured results, judgments, telemetry, receipts, and logs.

Add an owner-only preparation command:

```bash
python scripts/evaluate_bestplan.py freeze-cost-bounds \
  --runtime-config /absolute/private/path/to/eval-runtime.yaml \
  --tariff-contract /absolute/private/path/to/versioned-tariff-contract.json \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256

python scripts/evaluate_bestplan.py freeze-design \
  --runtime-config /absolute/private/path/to/eval-runtime.yaml \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256 \
  --factorial-eligible-cases /absolute/private/path/to/eligible-cases.jsonl \
  --lens-eligible-cases /absolute/private/path/to/eligible-lens-cases.jsonl \
  --target-population /absolute/private/path/to/target-population.json \
  --hypothesis-family bestplan-grounding-v2 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --factorial-selection-seed 20260723 \
  --lens-selection-seed 20260722 \
  --analysis-seed 20260721 \
  --calibration-seed 20260720 \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256

python scripts/evaluate_bestplan.py freeze-sampling-frames \
  --factorial-eligible-cases /absolute/private/path/to/eligible-cases.jsonl \
  --lens-eligible-cases /absolute/private/path/to/eligible-lens-cases.jsonl \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --strata category \
  --balance difficulty \
  --factorial-selection-seed 20260723 \
  --lens-selection-seed 20260722 \
  --factorial-pilot-families 20 \
  --lens-pilot-families 20 \
  --factorial-max-confirmatory-families 200 \
  --lens-max-confirmatory-families 200 \
  --factorial-pilot-cases-output /absolute/private/path/to/pilot-cases.jsonl \
  --lens-pilot-cases-output /absolute/private/path/to/lens-pilot-cases.jsonl \
  --hypothesis-family bestplan-grounding-v2 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --allocation-receipt /absolute/private/path/to/frame-allocation.json \
  --allocation-hash /absolute/private/path/to/frame-allocation.sha256 \
  --factorial-sampling-frame /absolute/private/path/to/factorial-sampling-frame.json \
  --factorial-sampling-hash /absolute/private/path/to/factorial-sampling-frame.sha256 \
  --lens-sampling-frame /absolute/private/path/to/lens-sampling-frame.json \
  --lens-sampling-hash /absolute/private/path/to/lens-sampling-frame.sha256

python scripts/evaluate_bestplan.py init \
  --cases /absolute/private/path/to/pilot-cases.jsonl \
  --condition-map /absolute/private/path/to/condition-map.json \
  --runtime-config /absolute/private/path/to/eval-runtime.yaml \
  --sampling-frame /absolute/private/path/to/factorial-sampling-frame.json \
  --sampling-hash /absolute/private/path/to/factorial-sampling-frame.sha256 \
  --frame-allocation-receipt /absolute/private/path/to/frame-allocation.json \
  --frame-allocation-hash /absolute/private/path/to/frame-allocation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256 \
  --design factorial_2x2 \
  --phase pilot \
  --attempt-count 5 \
  --seed 20260724

python scripts/evaluate_bestplan.py prepare-sources \
  --cases /absolute/path/to/redacted-cases.jsonl \
  --condition-map /absolute/private/path/to/condition-map.json \
  --workspace-corpus /absolute/private/path/to/workspaces \
  --source-input /absolute/private/path/to/approved-sources.json \
  --source-map /absolute/private/path/to/source-map.json \
  --source-corpus /absolute/private/path/to/source-corpus.json
```

`freeze-cost-bounds` strict-validates a versioned qualified tariff/contract and
derives per-dispatch upper bounds from the frozen input/output token,
iteration, and retry ceilings. Unknown provider bounds fail real-call
preflight. `freeze-design` is the only creator of the design manifest/hash. It requires a
clean source commit, both complete eligible populations, frozen target
population/inclusion/strata definitions, full runtime/treatment fingerprint,
the exact `splitmix64_v1`/`box_muller_v1` RNG contract and golden vectors, and
the fixed sizing-calibration rule and cost-bound digest. It also binds the current append-only
consumed-family ledger snapshot for the registered hypothesis family. Pilot
and confirmatory init only consume and verify it.

The single `freeze-sampling-frames` transaction runs before factorial pilot
IDs or output exist. It exact-validates
cutoffs/provenance, selects one registered representative per blinded family,
freezes inclusion/exclusion rules, target category weights, and
within-category difficulty balance, and
uses the two frozen family-stratified orders to create each 15/20-case pilot
and ordered holdout of at least 200 different families. It emits neither frame
unless factorial/lens pilot and holdout sets are mutually family-disjoint,
rejects any consumed/reserved family, atomically appends the whole joint
allocation to the ledger before emitting either frame, and cannot be rerun in
place. Ambiguous allocation crashes conservatively consume those families.

`init` is the only creator of the mode-`0600` condition map, random
`evaluation_id`, and opaque condition IDs. It consumes the immutable design
manifest/hash, requires the same clean exact source commit and explicit runtime profile,
rejects inline secrets and unsupported resolved modes, and freezes the full
experiment fingerprint: resolved model versions/order, count, synthesizer,
modes, prompts/lens schedules, candidate/parser/synthesis/tool/check/evidence/
telemetry/evaluator versions, and all limits. It binds the pre-pilot sampling
frame and both split
digests and never reads live `~/.hermes/config.yaml`. The one-case checked-in
sampling/design fixtures carry `fixture_only: true`; they are accepted only
with `--dry-run`, zero model dispatches, and no operator-approved flag. A real
runner or sizing command rejects them. `prepare-sources` verifies that
map and binds both generated artifacts to its exact evaluation ID and case-set
and design digests. It computes task/web provenance with the canonical helper, verifies every
approved file range against the immutable snapshot, binds exact bytes to its
safe source ID, rejects overlaps that could map one citation ambiguously, and
writes both files atomically as `0600` before model dispatch. Permit multiple
approved source versions only as separate non-overlapping records. A changed
file/manifest, URL/content, unknown fact, or out-of-range locator is
unmapped/unscoreable; never use a model or fuzzy matching to attach it to a
safe source.

- [ ] **Step 3: Define blinded adjudication**

Each private result fixture contains all bounded data under random evaluation
and condition IDs. It is never handed directly to a scorer:

```json
{
  "evaluation_id": "f93f43bb59a94cb8aa624ce5a4cff197",
  "design_kind": "factorial_2x2",
  "block_size": 4,
  "case_id": "synthetic-001",
  "blinded_condition_id": "7f2c37ce9db494186801f196478c101a",
  "seed": 20260724,
  "status": "success",
  "execution": {
    "scheduled_attempts": 5,
    "terminal_attempts": 5,
    "valid_candidates": 5,
    "candidate_invalid": 0,
    "timed_out": 0,
    "quorum_required": 4,
    "quorum_met": true,
    "synthesis_started": true,
    "synthesis_succeeded": true,
    "interrupted": false
  },
  "plan_artifact": {
    "body": "TL;DR\n- Use the configured explorer name.\n- Preserve dynamic explorer ordering.\n\nNext steps\n1. Change the default and run its validator.",
    "sha256": "body-sha256"
  },
  "plan_claims": [
    {
      "plan_claim_id": "plan-claim-001",
      "section": "tldr",
      "statement": "Use the configured explorer name."
    },
    {
      "plan_claim_id": "plan-claim-002",
      "section": "tldr",
      "statement": "Preserve dynamic explorer ordering."
    },
    {
      "plan_claim_id": "plan-claim-003",
      "section": "next_steps",
      "statement": "Change the default and run its validator."
    }
  ],
  "candidate_claims": [
    {
      "candidate_claim_id": "candidate-claim-001",
      "candidate_index": 0,
      "field": "steps[0]",
      "statement": "Change strongest to a configured explorer."
    }
  ],
  "evidence": [
    {
      "evidence_id": "evidence-001",
      "candidate_claim_id": "candidate-claim-001",
      "locator_status": "resolved",
      "support": "direct",
      "case_source_id": "source-001"
    }
  ],
  "findings": [
    {
      "finding_id": "finding-001",
      "statement": "Use the configured explorer name.",
      "candidate_indices": [0],
      "candidate_claim_ids": ["candidate-claim-001"]
    }
  ],
  "unscoreable_candidates": [],
  "telemetry": {}
}
```

All lists, identifiers, text, and artifact sizes are bounded. The
`plan_artifact.body` is the already validated compact body, and its hash must
match. `plan_claims` are enumerated only from TL;DR bullets, numbered Next
steps, and optional Risks bullets. Candidate claims/findings use only the
bounded shared `summary/steps/risks/verification` projection. V2 assumptions,
unknowns, and evidence are separate; production-valid V1 packets that cannot
be safely projected increment `unscoreable_candidates` without changing the
production run. Evaluation export persists only approved `case_source_id`
values in dedicated source fields, never raw locator paths/URLs, evidence
notes, receipts, host identity fields, tool traces, provider errors, or
credentials. The exact bounded model-authored plan/candidate text is the
artifact being scored and may legitimately mention a workspace-relative path,
public URL, or model name; do not redact it after hashing. Reject sentinel
secrets and keep that textual allowance distinct from hidden structured
metadata.

Export two separate exact-key scorer bundles:

- Phase A: evaluation/design/block IDs, case/condition/seed, safe case rubric
  and sources, `plan_artifact`, and `plan_claims` only; and
- Phase B: the same IDs, frozen Phase A consensus and raw-judgment-manifest
  SHA-256 values, the already frozen `plan_claims` as mapping anchors,
  `candidate_claims`, `evidence`, `findings`, `unscoreable_candidates`,
  execution projection, and telemetry.

Phase A contains no candidate schema/content, evidence array, execution state,
or telemetry. Phase B cannot be generated until Phase A judgments validate,
canonical consensus is derived, and its hash plus the raw-judgment manifest
hash are frozen. Cross-evaluation IDs, wrong design kind, wrong block size, or
a Phase B bundle/Phase A lineage mismatch fails closed.

The synthetic Phase A judgment fixture demonstrates:

```json
{
  "evaluation_id": "f93f43bb59a94cb8aa624ce5a4cff197",
  "design_kind": "factorial_2x2",
  "block_size": 4,
  "phase": "a",
  "case_id": "synthetic-001",
  "blinded_condition_id": "7f2c37ce9db494186801f196478c101a",
  "seed": 20260724,
  "score_group_id": "score-group-001",
  "judgment_id": "judgment-primary-a",
  "scorer": {
    "kind": "human",
    "id": "reviewer-a",
    "role": "primary"
  },
  "plan_success": true,
  "critical_facts_found": ["fact-id"],
  "constraints_preserved": ["must-preserve-id"],
  "forbidden_actions_triggered": [],
  "acceptance_checks_passed": ["observable-check-id"],
  "plan_claim_judgments": [
    {
      "plan_claim_id": "plan-claim-001",
      "critical": true,
      "factual": true,
      "factually_correct": true,
      "support_status": "supported"
    },
    {
      "plan_claim_id": "plan-claim-002",
      "critical": false,
      "factual": true,
      "factually_correct": true,
      "support_status": "unsupported"
    },
    {
      "plan_claim_id": "plan-claim-003",
      "critical": false,
      "factual": false,
      "factually_correct": null,
      "support_status": "not_applicable"
    }
  ],
  "plan_source_judgments": [
    {
      "plan_claim_id": "plan-claim-001",
      "case_source_id": "source-001",
      "entails_plan_claim": true
    }
  ]
}
```

The fixture includes a second record with the same `score_group_id`, distinct
`judgment_id`/human ID, and `role: primary`. Exact agreement across every
canonical scalar, sorted set, plan-claim judgment, and plan/source link derives
this separate host-owned record:

```json
{
  "evaluation_id": "f93f43bb59a94cb8aa624ce5a4cff197",
  "design_kind": "factorial_2x2",
  "block_size": 4,
  "phase": "a_consensus",
  "case_id": "synthetic-001",
  "blinded_condition_id": "7f2c37ce9db494186801f196478c101a",
  "seed": 20260724,
  "score_group_id": "score-group-001",
  "consensus_kind": "unanimous",
  "source_judgment_ids": [
    "judgment-primary-a",
    "judgment-primary-b"
  ],
  "controlling_judgment_id": "judgment-primary-a",
  "raw_judgments_manifest_sha256": "sha256-of-immutable-raw-file",
  "canonical_outcome_sha256": "sha256-of-canonical-outcome",
  "canonical_outcome": {
    "plan_success": true,
    "critical_facts_found": ["fact-id"],
    "constraints_preserved": ["must-preserve-id"],
    "forbidden_actions_triggered": [],
    "acceptance_checks_passed": ["observable-check-id"],
    "plan_claim_judgments": [
      {
        "plan_claim_id": "plan-claim-001",
        "critical": true,
        "factual": true,
        "factually_correct": true,
        "support_status": "supported"
      },
      {
        "plan_claim_id": "plan-claim-002",
        "critical": false,
        "factual": true,
        "factually_correct": true,
        "support_status": "unsupported"
      },
      {
        "plan_claim_id": "plan-claim-003",
        "critical": false,
        "factual": false,
        "factually_correct": null,
        "support_status": "not_applicable"
      }
    ],
    "plan_source_judgments": [
      {
        "plan_claim_id": "plan-claim-001",
        "case_source_id": "source-001",
        "entails_plan_claim": true
      }
    ]
  }
}
```

For unanimous groups the controlling judgment is the lexicographically first
primary ID after exact equality is proven; either record has identical
controlling fields. Every consensus record embeds the complete canonical
controlling outcome, its digest, the raw-judgment manifest hash, and ordered
source IDs. If any field differs, no unanimous consensus is emitted. A third
human record must use `scorer.role: adjudicator`, a new judgment ID, the same
group ID, and `resolves_judgment_ids` containing the exact two primary IDs. It
supplies all controlling fields, not only disputed ones; the host then derives
`consensus_kind: resolved_adjudication`, references all three records, names
the adjudicator record as controlling, and embeds its resolved canonical
outcome. Executable groups derive `consensus_kind: executable`, name the
closed executable judgment, and embed its canonical result only after
`scripts/bestplan_eval_checks.py` runs the versioned check with fixed
CPU/wall/memory limits, no network, no writable snapshot/checkout, and writes
a canonical code/input/output/limits/exit attestation. Validation re-executes
or independently verifies that attestation; a supplied executable JSON result
alone is never trusted. Unknown, nondeterministic, side-effecting, timed-out,
or non-zero-exit checks fail closed. The check registry/runner digest is part
of the experiment-design manifest. Raw score and attestation records
are append-only; only the validator writes consensus. Every consumer
hash-verifies the raw file and re-derives consensus before using the embedded
outcome.

The Phase B judgment for the same IDs contains only process judgments:

```json
{
  "evaluation_id": "f93f43bb59a94cb8aa624ce5a4cff197",
  "design_kind": "factorial_2x2",
  "block_size": 4,
  "phase": "b",
  "phase_a_consensus_sha256": "frozen-phase-a-consensus-hash",
  "case_id": "synthetic-001",
  "blinded_condition_id": "7f2c37ce9db494186801f196478c101a",
  "seed": 20260724,
  "score_group_id": "phase-b-score-group-001",
  "judgment_id": "phase-b-judgment-001",
  "scorer": {"kind": "human", "id": "reviewer-a", "role": "primary"},
  "candidate_plan_mappings": [
    {
      "candidate_claim_id": "candidate-claim-001",
      "plan_claim_ids": ["plan-claim-001"],
      "relation": "supports"
    }
  ],
  "candidate_claim_judgments": [
    {
      "candidate_claim_id": "candidate-claim-001",
      "critical": true,
      "grounding_status": "grounded"
    }
  ],
  "evidence_judgments": [
    {
      "evidence_id": "evidence-001",
      "candidate_claim_id": "candidate-claim-001",
      "case_source_id": "source-001",
      "locator_valid": true,
      "semantic_relation": "direct_support",
      "declared_class_matches": true
    }
  ],
  "finding_judgments": [
    {
      "finding_id": "finding-001",
      "valid": true,
      "gold_fact_ids": ["fact-id"],
      "conflict_group_id": "conflict-001",
      "minority_truth": true,
      "selected_in_plan": true
    }
  ]
}
```

The Phase B judgment fixture includes a second hidden primary record and any
required third adjudicator; the validator writes
`bestplan_eval_phase_b_consensus.example.jsonl` with the complete canonical
Stage B1/B2 outcome, ordered source IDs, raw-manifest hash, and outcome digest.

`scorer.kind` is exactly `human` or `executable`; human roles are
`primary|adjudicator`. Human IDs and executable check IDs are bounded
non-secret labels. A synthesizer/model self-score is not a valid scorer. Every
subjective Phase A and controlling Phase B artifact receives exactly two
independent primary human scores hidden from each other. Any canonical field
disagreement requires the linked third adjudicator. Summaries consume only derived
`unanimous|resolved_adjudication|executable` consensus records and report
field-level agreement/adjudication rates. Phase B cannot alter Phase A.

Plan-claim, candidate-claim, mapping, evidence-link, source, finding,
conflict-group, and gold-fact IDs must resolve within that exact evaluation
and case. A candidate claim can map to zero, one, or multiple plan claims, and
several candidate claims can map to one plan claim. This supports omission,
paraphrase, split, and merge without host semantic guessing. The condition,
source-map, and source-corpus files are separate from blinded bundles and
judgments and are not provided to scorers.

Every plan claim requires a Phase A judgment. Every factual plan claim marked
`supported` requires at least one direct safe-source link that entails it;
`unsupported`, `explicitly_unresolved`, and non-factual `not_applicable`
remain explicit. These arm-independent judgments control unsupported-claim
and critical-support metrics.

Phase B Stage B1 duplicate-scores/adjudicates candidate criticality,
candidate-to-plan mappings, and findings. Stage B2 then duplicate-scores the
complete census of evidence links attached to critical candidate claims plus
all declared counterevidence links. Its `semantic_relation` is exactly
`direct_support|indirect_support|contradicts|irrelevant_or_insufficient`;
counterevidence is class-consistent only when it contradicts. A frozen
probability sample of remaining noncritical direct/indirect links is
descriptive. `candidate_plan_mappings.relation` is
exactly `supports`, `counterevidence`, or `not_carried`; `not_carried` requires
an empty `plan_claim_ids` list, while the other relations require at least one
valid plan claim. Mapping records describe synthesis carry-through, not source
truth. Candidate-source semantic fidelity is a separate controlling grounding
gate and never grants automatic final-plan support.

- [ ] **Step 4: Write harness RED tests**

Using a fake BestPlan runner, prove:

- every run has a random opaque `evaluation_id` plus exact non-secret
  `design_kind` and `block_size`; cross-evaluation records, 4/6 mismatches, and
  a partial six-arm ablation presented as a complete 2-by-2 block are rejected;
- four condition configs are generated with identical models, count exactly
  five, and identical budget values, with count recorded in the protected map
  and each result;
- condition IDs are random opaque 128-bit values from an injectable secure
  generator, not raw labels or hashes of the four public condition
  definitions;
- only `init` creates the private condition map with owner-only permissions;
  `prepare-sources` binds its outputs to that evaluation ID/case digest, and
  `run` refuses missing, regenerated, or mismatched IDs;
- `freeze-design` is the only design-manifest creator; it binds the clean
  commit, full treatment/runtime/metric/RNG definition, target population/
  inclusion/category weights, and consumed-ledger snapshot, while init only
  consumes immutable inputs;
- one atomic `freeze-sampling-frames` transaction deterministically selects
  one representative case per family, creates factorial and lens pilot/
  ordered-holdout splits before factorial outputs, enforces target category
  allocation, within-category difficulty balance, cutoff/source provenance,
  at least three pilot families per category, and rejects cross-design family
  collision, post-resolution leakage, mutation, replenishment, reorder, or
  re-split;
- the append-only hypothesis-family ledger rejects any previously consumed or
  reserved family; joint frame allocation and confirmatory selection create
  fsynced receipts, ambiguous crashes consume conservatively, and a later
  prompt/source variant cannot reuse an unblinded family as fresh evidence;
- `init`/`run` require one explicit isolated runtime profile, reject inline
  keys/tokens and unsupported resolved modes, and freeze/verify a canonical
  experiment-design digest binding the exact clean commit, schemas/parsers,
  prompts/lens schedules, synthesis envelope, frozen tool/check registries,
  evidence/telemetry projections, evaluator metric/bootstrap code, resolved
  model versions/order, count, synthesizer, modes, and all limits without
  reading or changing live Hermes config;
- a sentinel live `~/.hermes/config.yaml` fixture is neither opened nor
  mutated, and changing the explicit profile after `init` fails before
  dispatch/resume;
- changing any design-manifest component between pilot, resume, sizing,
  confirmation, result export, or summary fails before dispatch/unblinding;
- the initialized map is reused on resume and is the only artifact containing
  raw condition settings;
- that map pre-registers evidence-only minus baseline as the primary evidence
  estimand, combined minus lenses-only as the required replication/interaction
  check, and combined minus evidence-only as the lens estimand;
- scorer-visible records contain no condition config or derivable condition
  digest;
- each seed derives one deterministic case order shared by all conditions;
- a frozen 5-by-5 Latin square assigns strategies/lenses to model positions,
  all arms in a block share a row, and only complete five-block cycles enter
  lens estimates;
- each `(case, seed)` is an atomic four-condition block;
- only condition order inside a block is deterministically shuffled;
- `--max-dispatches` counts exact host child dispatches and never claims to
  count provider HTTP attempts, internal app-server/subagent work, or legacy
  `api_calls`;
- the host budget contract derives seven dispatches per count-five condition,
  28 per four-condition block, and 42 per six-condition ablation block from
  five explorer children plus at most two fresh synthesizer children;
- a four-arm block is rejected at 27 remaining dispatches and admitted at
  exactly 28, while an ablation block is rejected at 41 and admitted at 42;
- `--max-dispatches`, `--max-cost-usd`, and
  `--worst-case-run-cost-usd` strict-parse integer/decimal input and reject
  booleans, NaN/infinity, negatives, fractional dispatches, overflow, and
  nonpositive real-call ceilings; exact boundaries pass and the next unit is
  refused;
- a paid block starts only when remaining host-dispatch budget and a
  tariff/contract-derived per-dispatch upper bound from frozen token/retry
  limits cover all runs; unknown upper bounds are dry-run-only, and all cost
  arithmetic uses `Decimal` or integer minor units rather than binary float;
- a block interrupted between conditions resumes its untouched conditions; a
  condition with any debit but no durable result is sealed interrupted rather
  than re-dispatched, and the block is excluded from paired summaries;
- resume preserves the original reservation, does not reserve the same block
  twice, and does not admit another block until the first is terminal;
- digest-verified durable condition results are resumable by
  case/condition/seed key without dispatch even when close/commit metadata is
  missing; any debit without such a result seals the condition interrupted
  instead of re-running it;
- crash fixtures cover after-dispatch, after-child-return/before-result-fsync,
  and after-result-fsync/before-close/commit windows without exceeding the
  reserved dispatch ceiling or tariff-derived hard-cost reservation;
- each JSONL record carries structured telemetry, status, validated compact
  body plus matching hash, and bounded plan-claim/candidate-claim/evidence/
  finding artifacts received through `_EvaluationArtifactSink`;
- each record carries the sink's exact identity-free execution projection,
  and success, candidate-invalid, quorum-failed, synthesis-failed, and timeout
  fixtures make valid-candidate, quorum, and synthesis rates computable
  without receipt parsing or inference from missing artifacts;
- the compact parser accepts only two-to-four TL;DR bullets, one-to-five
  numbered Next steps, and zero-to-three Risks bullets under the exact bare
  headings, and never invents a Verification section;
- artifact IDs are unique and all cross-references resolve;
- no raw receipt, dedicated host identity/locator/path/URL field, evidence
  note, unredacted source, tool trace, credential, or provider error is
  stored; bounded model-authored plan/candidate text is preserved unchanged;
- missing/unknown difficulty, workspace manifest mismatch, mutable snapshot
  drift, cross-case snapshot access, live web fallback, and any experimental
  `codex_app_server` or resolved xAI Responses runtime fail before a block
  starts;
- Phase A scorer bundles contain only case/safe-source rubric and compact
  plan/claims, with no candidate schema/content, evidence, execution, or
  telemetry;
- Phase B export is impossible until Phase A judgments validate, canonical
  consensus is derived, and its hash/raw-judgment manifest are frozen; a
  mismatched hash or any attempt to revise Phase A fails;
- subjective Phase A groups require exactly two distinct primary humans;
  exact canonical agreement derives a unanimous consensus record; any
  field-level disagreement requires one linked third adjudicator; executable
  groups derive an executable consensus; missing/extra/cross-group references
  fail; every consensus embeds and hashes the complete canonical controlling
  outcome and raw-manifest hash; consumers re-derive it from the verified raw
  file; executable consensus is accepted only from a closed versioned check
  registry plus sandboxed no-network/read-only attestation that validation
  re-executes or independently verifies; forged JSON/unknown/nondeterministic/
  side-effecting/timed-out checks fail; and only those three consensus kinds
  control summaries;
- pilot and confirmatory ceilings stop before admitting the next block;
- blinded judgments reject missing, duplicate, unknown-case, unknown-condition,
  wrong-seed, unknown plan-claim/candidate-claim/evidence/source/finding/
  gold-fact IDs, unbounded, and model-self-scored records;
- safe source text digests verify, executable check registry/code/input/output
  attestation digests resolve, and missing,
  duplicate, or mismatched owner-only source mappings fail before scoring;
- multiple observed task/web evidence items map independently through exact
  provenance; approved file ranges map exact/unambiguous subranges while
  superset, cross-boundary, ambiguous, and changed-byte fixtures fail closed;
- no dedicated raw locator/path/URL/query/call-ID/tool-result/provenance or
  host identity field appears in scorer-visible artifacts; exact bounded
  model-authored plan/candidate text may legitimately contain a path, public
  URL, or model name and is not silently redacted;
- candidate-to-plan adjudication handles paraphrase, several candidates merged
  into one plan claim, one candidate split across several plan claims, omitted
  candidate claims, and missing mappings that fail closed;
- Phase B Stage B1 and controlling Stage B2 each require two hidden primary
  humans plus exact consensus/third-adjudicator resolution; Stage B2 judges
  `direct_support|indirect_support|contradicts|irrelevant_or_insufficient`,
  treats valid counterevidence as contradiction rather than entailment, and
  censuses all critical-claim and declared-counterevidence links;
- the deterministic probability sample of remaining noncritical
  direct/indirect links is descriptive only; preflight computes worst-case and
  expected Phase A/B records plus scorer hours and refuses paid dispatch when
  explicit record/hour ceilings cannot cover every controlling judgment;
- a production-valid V1 packet may be marked evaluation-unscoreable without
  changing production success, and unscoreable-artifact rate is reported by
  condition;
- outcome metrics use every complete four-arm block intention-to-treat,
  including failed/unscoreable runs;
- a crash-sealed interrupted arm is charged the frozen
  `overall_timeout_ms` for p95 latency after resume, never null or a favorable
  partial duration;
- claim/evidence/finding contrasts use only blocks scoreable in every compared
  arm, report paired coverage/exclusions, and never use arm-specific
  denominators;
- promotion is impossible unless the one-sided 95% lower bound for paired
  scoreability is at least 90% and the one-sided upper bound for the
  highest-minus-lowest per-condition unscoreable-rate spread is at most 5
  percentage points;
- a selectively scoreable favorable arm cannot promote, and a favorable
  primary evidence contrast with a reversed replication contrast produces a
  deterministic no-promote result;
- neither primary nor replication may promote when the one-sided 95% upper
  bound on paired critical fact/constraint miss-rate increase exceeds `+2`
  percentage points, including a fixture where unsupported claims fall only
  because the treatment omits the critical claim;
- passing evidence deltas cannot promote when either evidence-only or combined
  misses the one-sided 95% lower-bound gates for 95% locator validity, 90%
  class-consistent semantic relations over the critical/counterevidence
  census, 90% grounded-or-explicitly-unresolved critical candidate claims, or
  90% supported-or-unresolved critical final-plan claims;
- paired metrics require one valid adjudication for every condition in a
  complete block;
- every declared primary metric can be computed from the validated artifacts
  and adjudication fields, including locator semantic-relation precision, critical
  support recall, unique findings, correlated misses, minority-truth
  retention, and wrong-majority selection;
- arm-independent direct plan/source judgments, not candidate evidence, control
  unsupported-claim and critical-support comparisons for all four arms;
- the scorer and judgment validator cannot access or infer the condition map;
  and
- confirmatory dispatch is impossible until pilot Phase A duplicate scoring,
  adjudication, and canonical consensus plus Phase B duplicate scoring,
  adjudication, and canonical consensus are complete, both raw/consensus
  hashes for both phases are frozen, and an owner-only sizing command freezes
  the maximum new-family sample size across every controlling gate;
- pilot cases/families and the ordered confirmatory holdout are selected from
  one frozen frame before pilot dispatch; fresh confirmation accepts only the
  first frozen `N` eligible representatives under the registered strata and
  verifies all snapshot/source/cutoff/design digests;
- missing easy cases, critical claims, evidence locators, known-cost coverage,
  or another denominator needed for sizing yields `inconclusive`;
- qualified cost per success uses one paired all-arms-cost-complete block set
  for both numerator and success denominator; an unknown-cost successful arm
  excludes the whole compared block and cannot bias the ratio downward;
- final `family_cluster_percentile_v1` bootstrap uses exactly 10,000 draws,
  the frozen analysis seed, Type-1 nearest-rank 5/95 or lens 2.5/97.5
  percentiles, samples exactly `n_h` families with replacement inside each
  frozen primary category, retains every case/seed/condition, aggregates with
  frozen target-category weights, rejects a category with fewer than three
  pilot families, and
  returns `inconclusive` on any undefined/non-finite controlling draw without
  retry, smoothing, BCa, basic, or studentized substitution; and
- bootstrap output records frozen method/version/draws/seed/quantile,
  sample-size methods, family/case/seed/easy-subset
  counts, point estimates, and one-sided confidence bounds for every
  controlling gate; and
- `cluster_power_calibration_v1` uses the frozen 20,000-replication,
  within-category, weighted, `.03` absolute-error/`.01` Monte Carlo half-width
  contract and byte-matches golden SplitMix64/index/Box-Muller vectors.

For the evaluation-only lens ablation, also prove:

- it refuses to plan or dispatch without an explicitly supplied positive
  2-by-2 summary and a separate operator approval marker/cost cap;
- it creates one full five-lens condition plus exactly five single-lens
  ablations from immutable host-owned schedules;
- every condition clones the combined treatment with
  `candidate_contract: evidence_v2`, `lens_contract: operational`, and count
  five; mixed candidate contracts or any change beyond the one closed lens
  mode are rejected;
- every run has count five and the same ordered host-mediated models, task,
  tools, per-stage limits, synthesizer, and dispatch/token ceilings;
- a frozen 5-by-5 Latin-square rotates every current strategy/operational lens
  across every model position; all arms in a `(case, seed)` block share one
  row and incomplete five-block cycles are excluded from lens estimates;
- each `(case, seed)` is an atomic six-run block with budget reserved for all
  six runs before dispatch;
- only condition order within that block changes;
- partial six-run blocks resume to completion or are excluded in full;
- it uses fresh random opaque IDs and a distinct protected condition map; and
- its protected map pre-registers the exact combined-minus-evidence
  prerequisite plus each full-minus-neutralized-lens estimand under the same
  alpha-split rule: `.025` success-superiority branch or `.025`
  success-non-inferiority-plus-critical-miss intersection-union branch, all
  using one-sided 97.5% bounds;
- the combined-minus-evidence prerequisite also requires one-sided 95%
  p95-latency and qualified-cost-per-success ratio upper bounds `<= 1.25`,
  plus a 95% lower bound on paired qualified-known-cost coverage; package
  efficiency is not redundantly tested in each leave-one-lens-out contrast;
- all five lenses must pass; mixed pass/fail fixtures and post-hoc category or
  metric selection produce deterministic `no_promote`; and
- no ablation setting is serialized into production BestPlan config or bound
  to a model; one pass applies only to its exact full experiment fingerprint,
  and broader recommendations require independent pre-registered coverage of
  every pool/version/order/count/synthesizer/mode/prompt/tool/limit and target
  task population/inclusion/strata dimension claimed rather than merely two
  pool orders.

- [ ] **Step 5: Run the RED tests**

Run:

```bash
scripts/run_tests.sh \
  tests/scripts/test_evaluate_bestplan.py \
  tests/agent/test_bestplan_orchestrator.py -q
```

Expected: FAIL because the harness does not exist.

- [ ] **Step 6: Implement block-matched dry-run and injected-runner modes**

Required CLI shape:

```bash
python scripts/evaluate_bestplan.py run \
  --cases /absolute/path/to/redacted-cases.jsonl \
  --output /absolute/path/to/results.jsonl \
  --condition-map /absolute/private/path/to/condition-map.json \
  --runtime-config /absolute/private/path/to/eval-runtime.yaml \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256 \
  --source-map /absolute/private/path/to/source-map.json \
  --source-corpus /absolute/private/path/to/source-corpus.json \
  --phase pilot \
  --seed 20260724 \
  --attempt-count 5 \
  --max-dispatches 560 \
  --max-cost-usd 50 \
  --worst-case-run-cost-usd 0.25 \
  --scoring-budget-records 50000 \
  --scoring-budget-hours 500 \
  --dry-run
```

`run` refuses to create an ID/map; it requires the initialized map and source
artifacts bound to its evaluation ID plus the exact design manifest/hash and
full experiment fingerprint. The runtime profile may use normal secret references, but
the harness never serializes resolved secret values and never falls back to
live config. `--dry-run` validates and prints host dispatches,
tariff-derived hard-cost reservations, Phase A/B worst-case and expected
judgment records, and scorer hours, then makes zero model calls. For each seed it shuffles
cases once, uses that common case order for all conditions, and randomizes only
the four condition positions inside each case block. It also freezes a
5-by-5 Latin-square rotation over attempt/model positions. Every arm in a
block uses the same row, and only complete five-block cycles enter lens-effect
analysis.

First make the existing host-mediated child iteration ceiling explicit without
changing behavior: replace `_build_child_agent`'s literal
`max_iterations=12` with a host-owned constant, define the compact synthesis
dispatch limit as two, and add a pure private evaluation helper:

```python
per_condition = (
    effective_count
    + BESTPLAN_SYNTHESIS_DISPATCH_LIMIT
)
```

At count five this is seven host child dispatches, not provider requests. The
four-arm block reservation is 28 and the six-arm reservation is 42.
`--max-dispatches` uses only this exact host-owned unit. Record the iteration
ceiling, synthesis-dispatch ceiling, dispatch formula, and derived values in
the canonical experiment-design manifest so any source/prompt/tool/evaluator
drift fails validation. Do not expose or budget
against legacy `api_calls`; post-budget summaries, transport retries, and
app-server subagents make it unsuitable as an underlying-request count.

The 2-by-2 lens experiment requires `--attempt-count 5`; any other count is
rejected rather than used to infer effects for lenses that did not run. Record
the exact count in the protected condition map and every result. All four arms
use that same count.

The preceding `init` command generates one `evaluation_id` and each
`blinded_condition_id` with `secrets.token_hex(16)` or equivalent 128-bit
cryptographic randomness, then writes the map atomically as mode `0600`.
`prepare-sources` and `run` reuse and validate it instead of regenerating IDs.
Never derive an ID from the condition label or public configuration. Every
record also carries exact non-secret `design_kind: factorial_2x2` and
`block_size: 4`. Lens-ablation `init` uses
`design_kind: lens_ablation`, `block_size: 6`, and a different evaluation ID.
No condition settings or condition hash appear in scorer bundles.

Before dispatch, the protected map also records the immutable decision
contrasts: evidence-only minus baseline as primary, combined minus lenses-only
as required evidence replication/interaction, and combined minus
evidence-only as the lens contrast. Resume rejects a map with different or
missing estimands. It records the exact lens prerequisite rule, Latin-square
rows/cycle membership, sampling-frame/split digests, and full experiment
fingerprint.

After the schema/leakage pilot has completed both validators, freeze separate
raw-judgment and consensus hashes for both Phase A and Phase B.
Before any confirmatory call, run `freeze-confirmatory-size` with all four
verified hashes/artifacts, pilot cases/results, and the protected condition map. It
may unblind only inside the owner-only calculation and emits no labels to
scorers. Pilot cases are excluded from later confirmatory analysis.

Freeze alpha `.05`, target power `.80`, one RNG seed, 10,000 pilot
family-cluster bootstrap draws, and candidate new-family counts
`25, 30, ..., 200` so every confirmatory run contains complete Latin cycles.
The frozen frame supplies exactly one dispatched representative case per
family, so family count equals case count. Do not nest bootstraps. Use family-cluster
influence/sandwich variance within each primary category for paired binary
differences, absolute rates,
log relative risks, and log mean/cost ratios; obtain p95 log-ratio and
simultaneous max-spread standard errors/critical values from the single pilot
bootstrap. Aggregate every point estimate and influence vector with the
pre-frozen target-category weights, preserve each stratum count `n_h`, and
require at least three pilot families per category. The one-seed pilot cannot estimate within-case seed correlation, so
pre-register `rho=1` and give the three confirmatory seeds no sizing credit.
Scale variance only by `n_pilot/n` and compute one-sided normal power
analytically for the exact joint gate at each grid value, where both `n`
values are independent-family counts. For the combined-minus-evidence lens
prerequisite, do not maximize separate marginal `.80` sizes: use 100,000
frozen `splitmix64_v1`/`box_muller_v1` multivariate-normal draws per grid value
from the weighted within-category joint covariance of success delta,
critical-miss delta, p95 log-latency ratio, log qualified-cost ratio, and
qualified-cost coverage. Require at least `.80` joint pass probability under
both quality pathways together with every efficiency/coverage guardrail;
singular or non-finite covariance is `inconclusive`.

The frozen sizing table is:

| Gate | Design alternative | Required one-sided decision |
|---|---:|---|
| Primary verified success | +10 pp | point >= +5 pp and lower bound > 0 |
| Replication verified success | +5 pp | lower bound > 0 |
| Primary unsupported-critical reduction | 40% | point >= 25% and lower bound >= 25% |
| Replication unsupported-critical reduction | 25% | lower bound > 0 |
| Primary and replication critical miss | 0 pp delta | upper bound on miss-rate increase <= +2 pp |
| Easy success | 0 pp delta | lower bound > -2 pp |
| p95 end-to-end latency ratio | 1.10 | upper bound <= 1.25 |
| Qualified cost-per-success ratio | 1.10 | upper bound <= 1.25 |
| Paired scoreability | 97% | lower bound >= 90% |
| Max condition unscoreable spread | 0 pp | upper bound <= 5 pp |
| Locator validity | 99% | lower bound >= 95% |
| Critical/counterevidence semantic relation match | 97% | lower bound >= 90% |
| Grounded-or-unresolved critical candidate claims | 97% | lower bound >= 90% |
| Qualified-known-cost coverage | 99% | lower bound >= 95% |
| Supported-or-unresolved critical claims | 97% | lower bound >= 90% |
| Lens prerequisite, success path | +5 pp success | one-sided 97.5% success lower bound > 0 |
| Lens prerequisite, NI/miss path | 0 pp success and +5 pp miss reduction | one-sided 97.5% success lower bound > -2 pp and miss-reduction lower bound > 0 |
| Lens prerequisite p95 latency ratio | 1.10 | upper bound <= 1.25 |
| Lens prerequisite qualified cost ratio | 1.10 | upper bound <= 1.25 with coverage lower bound >= 95% |

The lens prerequisite must have at least `.80` power under both pathway
alternatives; its eventual decision uses fixed branch alpha `.025/.025`, with
the NI/miss path an intersection-union test, jointly with the latency/cost/
coverage guardrails. The union is bounded by `.05`.

Record method version, seed, alternatives, thresholds, pilot variance/critical
values and family-cluster digest, per-gate selected count, required
easy/critical/evidence/known-cost denominators, final maximum, exact
sampling-frame/split/design digests, three confirmatory seeds, and
dispatch/cost/scorer-record/scorer-hour budgets.
Run pre-frozen `cluster_power_calibration_v1`: 20,000 replications per
controlling gate/grid point using `splitmix64_v1` rejection-sampled indices,
`box_muller_v1` normals, one fixed seed, within-category resampling that
preserves each `n_h` and target weight, centered pilot family influence
vectors plus the registered alternative, and no nested bootstrap. Every point
must have absolute analytic-versus-simulated power error `<= .03` and a
two-sided 95% Monte Carlo binomial half-width `<= .01`; otherwise sizing is
`inconclusive`. Golden uniform/index/normal vectors and one full calibration
result must byte-match. Missing/zero denominators, undefined
relative-risk baseline, no verified successes for cost-per-success,
non-finite/unstable variance, failure to reach `.80` by 200 cases, unsupported
runtime, or insufficient approved budget yields `inconclusive`; confirmatory
mode refuses dispatch and does not reinterpret no-promotion as no benefit.

Freeze final analysis as `family_cluster_percentile_v1`: exactly 10,000 draws
from `splitmix64_v1` and one recorded seed. In each draw sample exactly `n_h`
family IDs with replacement within each frozen primary category, retain all
cases/seeds/conditions, and recompute the target-population-weighted estimand
using the frozen category weights. Use Type-1
nearest-rank 5th/95th percentiles for alpha `.05` gates or 2.5th/97.5th
percentiles for the lens alpha-split gates. Do not use BCa/basic/studentized
intervals, smoothing, retries, or dropped draws. Any undefined/non-finite
controlling statistic in any draw returns `inconclusive`. Store method,
version, draws, seed, quantile, and family-manifest digest in the sizing report
and confirmatory map; summary must reproduce a golden fixture byte-for-byte.

Create confirmation through a fresh lineage-bound map:

```bash
python scripts/evaluate_bestplan.py select-confirmatory-cases \
  --sampling-frame /absolute/private/path/to/factorial-sampling-frame.json \
  --sampling-hash /absolute/private/path/to/factorial-sampling-frame.sha256 \
  --frame-allocation-receipt /absolute/private/path/to/frame-allocation.json \
  --frame-allocation-hash /absolute/private/path/to/frame-allocation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --sizing-report /absolute/private/path/to/frozen-sizing.json \
  --sizing-hash /absolute/private/path/to/frozen-sizing.sha256 \
  --cases-output /absolute/private/path/to/new-confirmatory-cases.jsonl \
  --selection-receipt /absolute/private/path/to/confirmatory-selection.json \
  --selection-hash /absolute/private/path/to/confirmatory-selection.sha256 \
  --reservation-receipt /absolute/private/path/to/confirmatory-reservation.json \
  --reservation-hash /absolute/private/path/to/confirmatory-reservation.sha256

python scripts/evaluate_bestplan.py init \
  --cases /absolute/private/path/to/new-confirmatory-cases.jsonl \
  --condition-map /absolute/private/path/to/confirmatory-map.json \
  --runtime-config /absolute/private/path/to/eval-runtime.yaml \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256 \
  --sampling-frame /absolute/private/path/to/factorial-sampling-frame.json \
  --sampling-hash /absolute/private/path/to/factorial-sampling-frame.sha256 \
  --selection-receipt /absolute/private/path/to/confirmatory-selection.json \
  --selection-hash /absolute/private/path/to/confirmatory-selection.sha256 \
  --frame-allocation-receipt /absolute/private/path/to/frame-allocation.json \
  --frame-allocation-hash /absolute/private/path/to/frame-allocation.sha256 \
  --reservation-receipt /absolute/private/path/to/confirmatory-reservation.json \
  --reservation-hash /absolute/private/path/to/confirmatory-reservation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --design factorial_2x2 \
  --phase confirmatory \
  --pilot-condition-map /absolute/private/path/to/pilot-map.json \
  --sizing-report /absolute/private/path/to/frozen-sizing.json \
  --sizing-hash /absolute/private/path/to/frozen-sizing.sha256 \
  --seeds 20260725,20260726,20260727 \
  --attempt-count 5

python scripts/evaluate_bestplan.py prepare-sources \
  --cases /absolute/private/path/to/new-confirmatory-cases.jsonl \
  --condition-map /absolute/private/path/to/confirmatory-map.json \
  --workspace-corpus /absolute/private/path/to/confirmatory-workspaces \
  --source-input /absolute/private/path/to/confirmatory-sources.json \
  --source-map /absolute/private/path/to/confirmatory-source-map.json \
  --source-corpus /absolute/private/path/to/confirmatory-source-corpus.json

python scripts/evaluate_bestplan.py run \
  --cases /absolute/private/path/to/new-confirmatory-cases.jsonl \
  --output /absolute/path/to/confirmatory-results.jsonl \
  --condition-map /absolute/private/path/to/confirmatory-map.json \
  --runtime-config /absolute/private/path/to/eval-runtime.yaml \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --reservation-receipt /absolute/private/path/to/confirmatory-reservation.json \
  --reservation-hash /absolute/private/path/to/confirmatory-reservation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --source-map /absolute/private/path/to/confirmatory-source-map.json \
  --source-corpus /absolute/private/path/to/confirmatory-source-corpus.json \
  --phase confirmatory \
  --sizing-report /absolute/private/path/to/frozen-sizing.json \
  --sizing-hash /absolute/private/path/to/frozen-sizing.sha256 \
  --attempt-count 5 \
  --max-dispatches <approved-frozen-value> \
  --max-cost-usd <approved-frozen-value> \
  --worst-case-run-cost-usd <tariff-derived-frozen-value> \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256 \
  --scoring-budget-records <approved-frozen-value> \
  --scoring-budget-hours <approved-frozen-value>
```

Confirmatory `init` verifies the pilot map/evaluation and all four scoring
hashes named by the sizing report. `select-confirmatory-cases` is the only
creator of the case file: it takes the first frozen `N` one-per-family IDs
under the registered strata/order and atomically appends a permanent
hypothesis-family reservation before emitting the case/selection receipts.
Any output, unblinding, or ambiguous crash leaves those families consumed;
future related designs cannot recycle them. Init rejects every pilot case/family,
requires the selection receipt and exactly three ordered seeds, verifies every
cutoff/snapshot/source digest plus the unchanged full experiment fingerprint,
and freezes a new case-set digest/evaluation ID. The map stores pilot-map,
sizing, sampling-frame, split, selection, design, and cost-bound digests,
frame-allocation/ledger/reservation receipts and digests, excluded IDs/
families, run/analysis seeds, and final-analysis method/version/draws/
quantiles. Every result and summary copies the non-secret ledger snapshot/
reservation digests. `run` and `summarize` require those same files; neither accepts
a pilot map, arbitrary cases, or implicit lineage.

Validate the owner-only source map against each case's bounded
`case_sources`. Each exact task/web provenance record and each approved
snapshot-backed file range must resolve to exactly one `case_source_id`, whose
redacted support-text digest verifies or whose executable check ID resolves in
the closed check registry. File exact/subset containment must be unique;
superset, cross-boundary, ambiguous, or changed-byte ranges fail. Live source
maps are mode `0600`. Results expose only the safe ID; they never expose
provenance, raw locator, query, call ID, tool result, path, or URL.

Verify each case's workspace manifest and create one immutable extracted root
before the first condition. Register `BestPlanEvaluationIO` with that root,
visible facts, and frozen web corpus for each derived task ID. Every arm uses
the same snapshot/corpus digests. A missing adapter, live fallback, mutable
file drift, cross-case access, `codex_app_server`, or resolved xAI Responses
runtime fails before dispatch. Tool definitions must expose the frozen web
adapter even without a live provider, and evaluation dispatch bypasses all
mutable middleware/plugin hooks.

Before a real block starts, reserve the host-dispatch ceiling and a hard cost
upper bound for all four condition runs. Derive every dispatch bound from the
frozen maximum input/output tokens, iteration/retry ceilings, and a versioned
qualified tariff/contract in `cost-bound-manifest`; an unknown upper bound is
dry-run-only and an operator-entered guess is never called a spend cap.
Strict-parse budget inputs (no bool, NaN/infinity, negative, fractional
dispatch, overflow, or nonpositive real-call limit) and perform currency
arithmetic in `Decimal` or integer minor units. Exact-boundary admission is
valid; the next block stops. Never stop voluntarily between conditions in an
admitted block. The real runner remains separately approval-gated by the
operator and by the frozen scorer-record/hour budget.

Persist the reservation before first dispatch, but do not treat reservation as
a crash-safe debit. Inject a private typed evaluation-budget callback at the
actual `_run_child_agent` seam. Immediately before each child call it appends
and fsyncs a unique
`(evaluation, block, condition, stage, attempt, dispatch_id)` debit; the call
cannot start until that succeeds. After terminal classification it appends a
bounded debit-completion record. After the whole condition finishes, append
and fsync the full bounded condition result plus digest atomically, then append
its condition-committed marker. A debit-completion record alone is never a
durable condition result. The protected map freezes how the tariff-derived
hard-cost reservation is allocated to each possible child dispatch.

After a complete block, reconcile closed debits and release capacity only
where qualified actual cost proves the refund. Unknown provider-internal work
never changes the host-dispatch unit; an uncertain debit keeps its qualified
upper-bound cost charged. On resume, reuse a digest-verified durable condition
result without dispatch even if a debit close or commit marker is missing, and
reconcile metadata only. If no verified condition result exists, any debit for
that condition—open or closed—counts as consumed and seals it
`crash_ambiguous`; it is never re-dispatched under the same reservation.
Derive a deterministic interrupted evaluation record from the journal, finish
the other condition arms within the original ceiling, and exclude the block
from claim/evidence paired analysis. Re-running requires a new evaluation/
block and fresh budget. Test required-minus-one, exact-boundary, ordinary
between-condition resume, unknown cost, crash after dispatch, crash after
child return but before condition-result fsync, and crash after result fsync
but before debit close/commit; observed dispatches/spend must never exceed
28/42 or the tariff-derived protected cost reservation.

Attach one typed evaluation sink per condition run and require its terminal
execution projection even when the run produces no valid plan artifact. Copy
only the exact identity-free counts/booleans into the result. A missing or
duplicate terminal projection makes the evaluation record invalid; never
recover these metrics by parsing a receipt or treating absent candidate
artifacts as invalid candidates. The sole exception is a journal-proven
`crash_ambiguous` condition, whose evaluation wrapper sets
`interrupted: true` and derives only bounded counts/classifications already
durably closed before the debit without a durable condition result; it never
fabricates a candidate result. Because monotonic time cannot span process
restart, set its `total_latency_ms` to the frozen per-condition
`overall_timeout_ms`, not null or a partial journal duration.

For each successful condition, validate the compact plan body, compute its
SHA-256, and obtain bounded candidate/admission projections only through the
private `_EvaluationArtifactSink`. Build plan claim IDs deterministically from
the exact compact grammar: TL;DR bullets, numbered Next steps, and optional
Risks bullets. There is no compact Verification section.

Build candidate claim and finding IDs only from the evaluation canonicalizer's
bounded shared `summary/steps/risks/verification` fields and candidate
indices; do not call another model to extract, map, or cluster them.
Evidence-link records exist only where Candidate V2 supplies evidence, and
they remain linked to `candidate_claim_id`, never directly to a synthesized
`plan_claim_id`. V2 assumptions and unknowns remain separate metrics.

A result without a valid plan artifact is not scoreable for claim metrics. A
production-valid V1 packet that the bounded evaluation canonicalizer rejects
does not change production success; record a bounded unscoreable code and
report its rate by condition. Host admission resolves exact task/web
provenance and approved file ranges through the protected source map before
the sink, which emits only approved safe source IDs. Structured export rejects
raw locators, source URLs/paths, queries, call IDs, tool results, host
provenance/identity, receipts, traces, errors, and secrets. It preserves the
already bounded model-authored plan/candidate text unchanged, including
legitimate textual path/URL/model references, because that is the artifact
being judged.

- [ ] **Step 7: Implement two-phase blinded adjudication**

Required flow:

```bash
python scripts/evaluate_bestplan.py export-phase-a \
  --cases /absolute/path/to/redacted-cases.jsonl \
  --results /absolute/path/to/private-results.jsonl \
  --bundle /absolute/path/to/phase-a-bundle.jsonl

python scripts/evaluate_bestplan.py validate-phase-a \
  --bundle /absolute/path/to/phase-a-bundle.jsonl \
  --judgments /absolute/path/to/phase-a-judgments.jsonl \
  --consensus /absolute/path/to/phase-a-consensus.jsonl \
  --raw-manifest-hash /absolute/path/to/phase-a-judgments.sha256 \
  --consensus-hash /absolute/path/to/phase-a-consensus.sha256

python scripts/evaluate_bestplan.py export-phase-b \
  --results /absolute/path/to/private-results.jsonl \
  --phase-a-consensus /absolute/path/to/phase-a-consensus.jsonl \
  --phase-a-hash /absolute/path/to/phase-a-consensus.sha256 \
  --phase-a-raw-manifest-hash /absolute/path/to/phase-a-judgments.sha256 \
  --bundle /absolute/path/to/phase-b-bundle.jsonl

python scripts/evaluate_bestplan.py validate-phase-b \
  --cases /absolute/path/to/redacted-cases.jsonl \
  --bundle /absolute/path/to/phase-b-bundle.jsonl \
  --judgments /absolute/path/to/phase-b-judgments.jsonl \
  --consensus /absolute/path/to/phase-b-consensus.jsonl \
  --raw-manifest-hash /absolute/path/to/phase-b-judgments.sha256 \
  --consensus-hash /absolute/path/to/phase-b-consensus.sha256

python scripts/evaluate_bestplan.py freeze-confirmatory-size \
  --cases /absolute/path/to/pilot-cases.jsonl \
  --results /absolute/path/to/pilot-results.jsonl \
  --sampling-frame /absolute/private/path/to/factorial-sampling-frame.json \
  --sampling-hash /absolute/private/path/to/factorial-sampling-frame.sha256 \
  --frame-allocation-receipt /absolute/private/path/to/frame-allocation.json \
  --frame-allocation-hash /absolute/private/path/to/frame-allocation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --phase-a-judgments /absolute/path/to/phase-a-judgments.jsonl \
  --phase-a-raw-manifest-hash /absolute/path/to/phase-a-judgments.sha256 \
  --phase-a-consensus /absolute/path/to/phase-a-consensus.jsonl \
  --phase-a-hash /absolute/path/to/phase-a-consensus.sha256 \
  --phase-b-judgments /absolute/path/to/phase-b-judgments.jsonl \
  --phase-b-raw-manifest-hash /absolute/path/to/phase-b-judgments.sha256 \
  --phase-b-consensus /absolute/path/to/phase-b-consensus.jsonl \
  --phase-b-consensus-hash /absolute/path/to/phase-b-consensus.sha256 \
  --condition-map /absolute/private/path/to/condition-map.json \
  --sizing-report /absolute/private/path/to/frozen-sizing.json \
  --sizing-hash /absolute/private/path/to/frozen-sizing.sha256
```

Phase A exports only case/safe-source rubric and compact plan/claims. It
rejects candidate schema/content, evidence, execution, telemetry, and model
identity. Every subjective artifact requires two independent human records
with one score-group ID and hidden peer scores. Exact canonical agreement
derives unanimous consensus; any field-level disagreement requires a distinct
linked adjudicator record; executable outcomes require the closed registry
attestation. Each phase reports field-level agreement/adjudication, writes
canonical consensus, and freezes both its raw-judgment manifest and consensus
hashes.

Phase B export requires both Phase A hashes and exposes only the bounded process
artifact plus frozen plan-claim anchors. It contains no Phase A scores,
safe-source judgments, or consensus outcomes and cannot overwrite Phase A.
Phase B subjective records use the same hidden two-primary/third-adjudicator
derivation and are consumed only through re-derived canonical Phase B
consensus. Both validators reject
synthesizer/model scoring, duplicates, mismatched evaluation/design/block
identity, partial 4/6 blocks, body/hash mismatches, unknown IDs, and incomplete
judgments. Phase B additionally validates paraphrase, zero/one/many
candidate-to-plan mappings, safe-source evidence links, merge/split cases, and
conflict groups. Neither validator reads the condition map.

Only after the complete pilot passes both validators and freezes all four
raw/consensus hashes does the owner-only
sizing command read the protected map. It freezes the maximum required
new-family sample size and denominator/precision targets across every
controlling gate, records all pilot case/family IDs plus both pre-pilot split
digests, and emits no condition labels. Confirmatory selection accepts only
the first frozen `N` eligible representatives under the registered strata.
Confirmatory `run` rejects reused families, an absent/changed sizing or design
record, sparse unsized denominators, and insufficient dispatch/cost/scoring
budget.

- [ ] **Step 8: Implement summaries**

Required summary command:

```bash
python scripts/evaluate_bestplan.py summarize \
  --cases /absolute/path/to/redacted-cases.jsonl \
  --results /absolute/path/to/results.jsonl \
  --phase-a-judgments /absolute/path/to/phase-a-judgments.jsonl \
  --phase-a-raw-manifest-hash /absolute/path/to/phase-a-judgments.sha256 \
  --phase-a-consensus /absolute/path/to/phase-a-consensus.jsonl \
  --phase-a-hash /absolute/path/to/phase-a-consensus.sha256 \
  --phase-b-judgments /absolute/path/to/phase-b-judgments.jsonl \
  --phase-b-raw-manifest-hash /absolute/path/to/phase-b-judgments.sha256 \
  --phase-b-consensus /absolute/path/to/phase-b-consensus.jsonl \
  --phase-b-consensus-hash /absolute/path/to/phase-b-consensus.sha256 \
  --condition-map /absolute/private/path/to/condition-map.json \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256 \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --sampling-frame /absolute/private/path/to/factorial-sampling-frame.json \
  --sampling-hash /absolute/private/path/to/factorial-sampling-frame.sha256 \
  --selection-receipt /absolute/private/path/to/confirmatory-selection.json \
  --selection-hash /absolute/private/path/to/confirmatory-selection.sha256 \
  --reservation-receipt /absolute/private/path/to/confirmatory-reservation.json \
  --reservation-hash /absolute/private/path/to/confirmatory-reservation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --sizing-report /absolute/private/path/to/frozen-sizing.json \
  --sizing-hash /absolute/private/path/to/frozen-sizing.sha256 \
  --summary /absolute/path/to/summary.json
```

Only this post-adjudication command unblinds condition IDs.
It first re-derives both Phase A and Phase B consensus from their verified raw
manifests and verifies the design, sampling/selection, sizing, analysis-method,
RNG, consumed-family ledger/reservation, target-population weights, and full
experiment-fingerprint lineage.

Build two explicit analysis sets:

1. intention-to-treat contains every complete four-condition `(case, seed)`
   block for outcome, execution, latency, and cost metrics, including failed
   or evaluation-unscoreable runs; and
2. paired complete-scoreability contains only blocks where every compared arm
   has a valid plan artifact, every production-valid candidate has a bounded
   evaluation projection, and every required judgment is complete.

A production-invalid candidate remains in the intention-to-treat execution
denominator and is a real no-finding result, not attrition. Claim/evidence/
finding contrasts use only the second set with one shared paired denominator.
Report excluded block IDs/counts, total paired coverage, and per-condition
unscoreable rates.

All controlling overall estimands use the target-category weights frozen
before pilot. Final bootstrap/sizing resample within category and preserve
each `n_h`; the easy subset uses its separately frozen target category weights.
A missing weighted stratum is `inconclusive`, never silently reweighted to the
observed sample mix.

Compute:

- valid-candidate, quorum, synthesis, and plan success rates;
- paired plan-success rate on the pre-frozen `difficulty: easy` subset, with
  its case/block denominator and scoreability coverage;
- critical fact/constraint miss rates;
- unsupported critical-plan-claim rate and critical support/unresolved recall
  from arm-independent Phase A direct plan/source judgments;
- evidence locator resolution from host status, semantic-relation match
  precision over the complete critical-claim/counterevidence census, and
  grounded-or-explicitly-unresolved critical candidate-claim coverage from
  canonical Phase B consensus;
- class-stratified candidate-evidence fidelity and synthesis retention as
  separate Phase B measures; the semantic floors control grounding promotion
  but never grant automatic final-plan support credit;
- unique valid findings and correlated misses across candidate indices;
- minority-truth retention and wrong-majority selection within adjudicated
  conflict groups;
- evaluation-unscoreable candidate rate by condition;
- p50/p95 end-to-end host `total_latency_ms` over every intention-to-treat
  condition run, including failure, timeout, and interruption at terminal
  classification; child or summed-child latency is diagnostic only;
- tokens;
- qualified cost per verified success with complete-usage/independent-cost
  provenance on one paired cost-complete block set, plus that set's ITT
  coverage; and
- paired deltas from exact `family_cluster_percentile_v1`: 10,000 draws from
  the frozen analysis seed, sampling family IDs and retaining all cases/seeds/
  conditions, with Type-1 nearest-rank 5/95 or lens 2.5/97.5 percentiles and
  fail-closed non-finite-draw handling.

Each metric fails closed when required artifact or judgment fields are
missing. The decision record names the pre-registered primary, replication,
and lens contrasts from the protected map. Evidence promotion applies every
delta threshold—verified success, unsupported critical claims, easy-task
regression, critical fact/constraint miss non-inferiority, latency, and
cost—to evidence-only minus baseline using the
pre-registered point and one-sided confidence-bound rules. Combined minus
lenses-only must have favorable one-sided bounds for verified success and
unsupported critical claims and an upper bound on critical-miss increase no
greater than `+2` percentage points. Separately, both evidence-only and combined must
each have a one-sided 95% lower bound of at least 95% valid admitted locators
and at least 90% class-consistent semantic relations over the complete
critical/counterevidence census, at least 90% critical candidate claims with
class-consistent admitted evidence or explicit unresolved status, and at
least 90% critical factual plan claims supported by direct Phase A plan/source
judgments or explicitly unresolved. Pre-frozen class applicability prevents a
missing natural class from becoming a quota; class strata without adequate
opportunities are descriptive. A discordant replication or failed absolute
bound is `no_promote`, even when primary point estimates pass.

Promotion also requires a one-sided 95% lower bound of at least 90% paired
scoreability and an upper bound of at most 5 percentage points for the maximum
minus minimum per-condition unscoreable-rate spread. Easy-task
non-inferiority uses only frozen `difficulty: easy` cases and requires its
lower bound above -2 points. Latency and cost require upper bounds on relative
increase no greater than 25%; cost additionally requires a one-sided 95% lower
bound of at least 95% paired qualified-known-cost coverage in every compared
arm. A denominator or coverage failure that prevents the frozen precision
target is `inconclusive`; a sufficiently precise bound that misses a gate is
`no_promote`.

Compute qualified cost per success as total complete-coverage qualified run
cost divided by verified successes using the *same* paired cost-complete
blocks, where every compared arm has qualified complete cost. Report coverage
against all ITT blocks and require its one-sided lower bound to reach 95%.
Never include an unknown-cost success only in the denominator. For the
treatment/baseline ratio, two exact qualified zeros define ratio `1`; zero
baseline with positive treatment defines `+infinity` and fails; positive
baseline with zero treatment defines `0`. Contractually included zero remains
known and is never treated as missing. No verified successes in the paired
cost-complete set makes the gate unsized/inconclusive, as frozen by the sizing
record.

The summarizer may not impute missing artifacts, choose an arm-specific
denominator, or select a favorable contrast after unblinding. Do not replace
human or executable acceptance with synthesizer self-scoring.

- [ ] **Step 9: Implement the separately gated five-lens ablation**

Required dry-run shape:

```bash
python scripts/evaluate_bestplan.py run-lens-ablation \
  --cases /absolute/path/to/redacted-cases.jsonl \
  --promotion-summary /absolute/path/to/positive-2x2-summary.json \
  --output /absolute/path/to/ablation-results.jsonl \
  --condition-map /absolute/private/path/to/ablation-condition-map.json \
  --runtime-config /absolute/private/path/to/eval-runtime.yaml \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256 \
  --source-map /absolute/private/path/to/source-map.json \
  --source-corpus /absolute/private/path/to/source-corpus.json \
  --seed 20260724 \
  --attempt-count 5 \
  --max-dispatches 840 \
  --max-cost-usd 75 \
  --worst-case-run-cost-usd 0.25 \
  --scoring-budget-records 50000 \
  --scoring-budget-hours 500 \
  --phase pilot \
  --operator-approved \
  --dry-run
```

The shown 20-family pilot derives `20 * 42 = 840`; a 15-family frame derives
630. The harness computes and asserts this from the immutable frame rather
than trusting a copied literal.

Validate that the supplied summary records combined minus evidence-only
passing the exact pre-registered alpha-split rule: either verified-success has
a one-sided 97.5% lower bound above zero, or verified-success is non-inferior
with a one-sided 97.5% lower bound above `-2` percentage points *and*
critical-miss reduction has a one-sided 97.5% lower bound above zero. The two
branches receive fixed alpha `.025/.025`; the second branch is an
intersection-union test. A point improvement or one favorable
metric alone is rejected. The same summary must show combined/evidence-only
p95-latency and qualified-cost-per-success ratio upper bounds `<= 1.25` and a
qualified-known-cost coverage lower bound `>= 95%`. It must be a terminal confirmatory summary, not a
pilot, and its design/condition-map hash, attempt count five, evidence
contract, and exact full experiment fingerprint must match the
planned ablation preflight. A pool-A summary cannot authorize pool B. The
explicit `--operator-approved` flag is still required; it acknowledges only
the declared ablation budget and does not authorize live configuration
changes.

Use the separate lens sampling frame/split that Task 8 froze before the
factorial pilot. It already selected 15/20 one-per-family pilot cases and an
ordered disjoint holdout of at least 200 families under frozen
primary category strata/target weights, within-category difficulty balance,
cutoff/provenance, and split digests. Bind that
immutable frame, the positive 2-by-2 summary, and the same exact full
experiment fingerprint into a fresh
`init --design lens_ablation --phase pilot`; then prepare its own source
corpus. That init consumes the same design and cost-bound manifests/hashes.
Neither pilot nor holdout may be handpicked, replaced, or replenished
after seeing factorial results.

After the same two-phase scoring freezes Phase A raw/consensus and Phase B
raw/consensus hashes, run:

```bash
python scripts/evaluate_bestplan.py freeze-lens-size \
  --cases /absolute/private/path/to/lens-pilot-cases.jsonl \
  --results /absolute/path/to/lens-pilot-results.jsonl \
  --sampling-frame /absolute/private/path/to/lens-sampling-frame.json \
  --sampling-hash /absolute/private/path/to/lens-sampling-frame.sha256 \
  --frame-allocation-receipt /absolute/private/path/to/frame-allocation.json \
  --frame-allocation-hash /absolute/private/path/to/frame-allocation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --phase-a-judgments /absolute/path/to/lens-phase-a-judgments.jsonl \
  --phase-a-raw-manifest-hash /absolute/path/to/lens-phase-a-judgments.sha256 \
  --phase-a-consensus /absolute/path/to/lens-phase-a-consensus.jsonl \
  --phase-a-hash /absolute/path/to/lens-phase-a-consensus.sha256 \
  --phase-b-judgments /absolute/path/to/lens-phase-b-judgments.jsonl \
  --phase-b-raw-manifest-hash /absolute/path/to/lens-phase-b-judgments.sha256 \
  --phase-b-consensus /absolute/path/to/lens-phase-b-consensus.jsonl \
  --phase-b-consensus-hash /absolute/path/to/lens-phase-b-consensus.sha256 \
  --condition-map /absolute/private/path/to/lens-pilot-map.json \
  --sizing-report /absolute/private/path/to/frozen-lens-sizing.json \
  --sizing-hash /absolute/private/path/to/frozen-lens-sizing.sha256
```

The command uses only complete five-block Latin cycles. It freezes
family-wise alpha `.05`, target all-five-package power `.80`, one RNG seed,
10,000 pilot family-cluster bootstrap draws to estimate the joint covariance
of each lens's success and critical-miss deltas, 100,000 draws from that
frozen multivariate normal per candidate size, and the independent-family grid
`25, 30, ..., 200` (one dispatched representative case per family). This is
one covariance estimate plus analytic simulation, not nested bootstrap. As in
the 2-by-2 sizing, the one-seed pilot uses `rho=1`, so confirmatory repeat
seeds earn no sizing credit. Every covariance and draw preserves the primary
category `n_h` values and frozen target weights.

At each grid value, simulate the exact all-five alpha-split rule under both
pre-registered package alternatives:

- success pathway: every lens has `+5` percentage-point verified-success
  delta and zero critical-miss reduction; and
- non-inferiority/miss pathway: every lens has zero success delta and `+5`
  percentage-point critical-miss reduction.

For a simulated lens, branch 1 uses its assigned alpha `.025` and passes only
when the one-sided 97.5% success lower bound is above zero. Branch 2 uses the
other `.025` and, as an intersection-union test, requires both one-sided 97.5%
bounds: success above `-2` percentage points and critical-miss reduction above
zero.
The package passes only when all five
lenses pass. Select the smallest
multiple-of-five family count with at least `.80` joint package power in *both*
alternative scenarios. Freeze method version, seed, covariance/digest,
alternatives, per-size powers, chosen size, sampling/split/design/full-
fingerprint digests, final-bootstrap method/seed, and dispatch/cost/scorer
budget. Final intervals use the same 10,000-draw
`family_cluster_percentile_v1` contract and 2.5/97.5 lens quantiles.

Pilot cases/families are excluded from confirmatory ablation. Missing/zero critical-
miss or success denominators, non-finite/singular covariance that the fixed
method cannot handle, fewer than three complete pilot Latin cycles, failure to
reach `.80` by 200 families, or unfunded 42-dispatch-per-case and complete
controlling-judgment budgets yields `inconclusive`; confirmatory ablation
cannot start without the frozen record.

Use a fresh confirmation lineage:

```bash
python scripts/evaluate_bestplan.py select-confirmatory-cases \
  --sampling-frame /absolute/private/path/to/lens-sampling-frame.json \
  --sampling-hash /absolute/private/path/to/lens-sampling-frame.sha256 \
  --frame-allocation-receipt /absolute/private/path/to/frame-allocation.json \
  --frame-allocation-hash /absolute/private/path/to/frame-allocation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --sizing-report /absolute/private/path/to/frozen-lens-sizing.json \
  --sizing-hash /absolute/private/path/to/frozen-lens-sizing.sha256 \
  --cases-output /absolute/private/path/to/lens-confirmatory-cases.jsonl \
  --selection-receipt /absolute/private/path/to/lens-confirmatory-selection.json \
  --selection-hash /absolute/private/path/to/lens-confirmatory-selection.sha256 \
  --reservation-receipt /absolute/private/path/to/lens-confirmatory-reservation.json \
  --reservation-hash /absolute/private/path/to/lens-confirmatory-reservation.sha256

python scripts/evaluate_bestplan.py init \
  --cases /absolute/private/path/to/lens-confirmatory-cases.jsonl \
  --condition-map /absolute/private/path/to/lens-confirmatory-map.json \
  --runtime-config /absolute/private/path/to/eval-runtime.yaml \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256 \
  --sampling-frame /absolute/private/path/to/lens-sampling-frame.json \
  --sampling-hash /absolute/private/path/to/lens-sampling-frame.sha256 \
  --selection-receipt /absolute/private/path/to/lens-confirmatory-selection.json \
  --selection-hash /absolute/private/path/to/lens-confirmatory-selection.sha256 \
  --frame-allocation-receipt /absolute/private/path/to/frame-allocation.json \
  --frame-allocation-hash /absolute/private/path/to/frame-allocation.sha256 \
  --reservation-receipt /absolute/private/path/to/lens-confirmatory-reservation.json \
  --reservation-hash /absolute/private/path/to/lens-confirmatory-reservation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --promotion-summary /absolute/path/to/positive-2x2-summary.json \
  --design lens_ablation \
  --phase confirmatory \
  --pilot-condition-map /absolute/private/path/to/lens-pilot-map.json \
  --sizing-report /absolute/private/path/to/frozen-lens-sizing.json \
  --sizing-hash /absolute/private/path/to/frozen-lens-sizing.sha256 \
  --seeds 20260725,20260726,20260727 \
  --attempt-count 5

python scripts/evaluate_bestplan.py prepare-sources \
  --cases /absolute/private/path/to/lens-confirmatory-cases.jsonl \
  --condition-map /absolute/private/path/to/lens-confirmatory-map.json \
  --workspace-corpus /absolute/private/path/to/lens-confirmatory-workspaces \
  --source-input /absolute/private/path/to/lens-confirmatory-sources.json \
  --source-map /absolute/private/path/to/lens-confirmatory-source-map.json \
  --source-corpus /absolute/private/path/to/lens-confirmatory-source-corpus.json

python scripts/evaluate_bestplan.py run-lens-ablation \
  --cases /absolute/private/path/to/lens-confirmatory-cases.jsonl \
  --promotion-summary /absolute/path/to/positive-2x2-summary.json \
  --output /absolute/path/to/lens-confirmatory-results.jsonl \
  --condition-map /absolute/private/path/to/lens-confirmatory-map.json \
  --runtime-config /absolute/private/path/to/eval-runtime.yaml \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --reservation-receipt /absolute/private/path/to/lens-confirmatory-reservation.json \
  --reservation-hash /absolute/private/path/to/lens-confirmatory-reservation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --source-map /absolute/private/path/to/lens-confirmatory-source-map.json \
  --source-corpus /absolute/private/path/to/lens-confirmatory-source-corpus.json \
  --phase confirmatory \
  --sizing-report /absolute/private/path/to/frozen-lens-sizing.json \
  --sizing-hash /absolute/private/path/to/frozen-lens-sizing.sha256 \
  --attempt-count 5 \
  --max-dispatches <approved-frozen-value> \
  --max-cost-usd <approved-frozen-value> \
  --worst-case-run-cost-usd <tariff-derived-frozen-value> \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256 \
  --scoring-budget-records <approved-frozen-value> \
  --scoring-budget-hours <approved-frozen-value> \
  --operator-approved

python scripts/evaluate_bestplan.py summarize-lens \
  --cases /absolute/private/path/to/lens-confirmatory-cases.jsonl \
  --results /absolute/path/to/lens-confirmatory-results.jsonl \
  --phase-a-judgments /absolute/path/to/lens-phase-a-judgments.jsonl \
  --phase-a-raw-manifest-hash /absolute/path/to/lens-phase-a-judgments.sha256 \
  --phase-a-consensus /absolute/path/to/lens-phase-a-consensus.jsonl \
  --phase-a-hash /absolute/path/to/lens-phase-a-consensus.sha256 \
  --phase-b-judgments /absolute/path/to/lens-phase-b-judgments.jsonl \
  --phase-b-raw-manifest-hash /absolute/path/to/lens-phase-b-judgments.sha256 \
  --phase-b-consensus /absolute/path/to/lens-phase-b-consensus.jsonl \
  --phase-b-consensus-hash /absolute/path/to/lens-phase-b-consensus.sha256 \
  --condition-map /absolute/private/path/to/lens-confirmatory-map.json \
  --cost-bound-manifest /absolute/private/path/to/cost-bounds.json \
  --cost-bound-hash /absolute/private/path/to/cost-bounds.sha256 \
  --design-manifest /absolute/private/path/to/experiment-design.json \
  --design-hash /absolute/private/path/to/experiment-design.sha256 \
  --sampling-frame /absolute/private/path/to/lens-sampling-frame.json \
  --sampling-hash /absolute/private/path/to/lens-sampling-frame.sha256 \
  --selection-receipt /absolute/private/path/to/lens-confirmatory-selection.json \
  --selection-hash /absolute/private/path/to/lens-confirmatory-selection.sha256 \
  --reservation-receipt /absolute/private/path/to/lens-confirmatory-reservation.json \
  --reservation-hash /absolute/private/path/to/lens-confirmatory-reservation.sha256 \
  --consumed-family-ledger /absolute/private/path/to/consumed-families.jsonl \
  --consumed-ledger-hash /absolute/private/path/to/consumed-families.sha256 \
  --sizing-report /absolute/private/path/to/frozen-lens-sizing.json \
  --sizing-hash /absolute/private/path/to/frozen-lens-sizing.sha256 \
  --summary /absolute/path/to/lens-confirmatory-summary.json
```

Fresh lens init verifies the four pilot scoring hashes through the sizing
lineage, first-`N` selection receipt, disjoint families, exact frozen size,
atomic consumed-family reservation/ledger lineage, three run seeds,
final-bootstrap seed/method, target weights, and unchanged full experiment
fingerprint. Results carry the reservation digest; run and summary refuse any
changed or missing artifact before dispatch or unblinding.

Generate one full operational schedule and five schedules that each neutralize
one checklist using Task 7's closed `_EvaluationLensSchedule`. Keep count five,
`candidate_contract: evidence_v2`, all five attempt positions, ordered models,
task, tools, stage limits, synthesizer, and token/dispatch ceilings identical.
All six clone the combined treatment with `lens_contract: operational` and
differ only in the one closed lens mode. Store and validate these invariants
with fresh random opaque IDs in a separate mode-`0600` map; reject any mixed
candidate contract, count, model, budget, tool, or free-form prompt.

Freeze the same 5-by-5 Latin-square rotation used by the 2-by-2 design. All
six arms in a block share a row; across a complete five-block cycle every lens
appears once at every model position. Exclude incomplete cycles from lens
estimates. Bind any opt-in recommendation to the exact full experiment
fingerprint. A broader recommendation requires independent,
pre-registered confirmation across every pool/version/order/count/synthesizer/
mode/prompt/tool/limit and target task population/inclusion/strata dimension
it claims to generalize; two pool orders or one narrow task frame alone do not
justify arbitrary flexible arrays or population-wide defaults.

For each `(case, seed)`, reserve the exact 42 host dispatches, the six-run
tariff-derived hard-cost upper bound, and the complete controlling scoring budget
before dispatch, randomize only the six condition positions, and treat the
block atomically for resume and summary. The same blinded artifacts,
adjudication schema, fail-closed metrics, and
`family_cluster_percentile_v1` rules apply.
Before dispatch, record five fixed full-minus-neutralized-lens estimands and
this fixed alpha-split decision rule:

1. the alpha-`.025` success branch passes when verified plan success improves
   with a one-sided 97.5% lower bound above zero; otherwise
2. the separate alpha-`.025` intersection-union branch passes only when
   verified plan success is non-inferior with a one-sided 97.5% lower bound
   above -2 percentage points and critical-miss reduction has a one-sided
   97.5% lower bound above zero.

Package-level p95 latency/cost/coverage was already required by the
combined-minus-evidence prerequisite and is not re-tested five times here.
All five lenses must pass. A mixed result is `no_promote`; task-category
strata are descriptive and cannot replace the overall controlling metrics.
Do not remove a failed lens or choose a favorable metric after unblinding. A
reduced package requires a new pre-registered design and fresh confirmation.
This phase is evaluation-only and creates no production config or model-role
binding.

- [ ] **Step 10: Run the GREEN tests**

Run the command from Step 5.

Expected: PASS.

- [ ] **Step 11: Run only the synthetic 2-by-2 dry run**

Run:

```bash
python scripts/evaluate_bestplan.py init \
  --cases tests/fixtures/bestplan_eval_cases.example.jsonl \
  --condition-map /tmp/bestplan-eval-condition-map.json \
  --runtime-config tests/fixtures/bestplan_eval_runtime.example.yaml \
  --sampling-frame tests/fixtures/bestplan_eval_sampling_frame.example.json \
  --sampling-hash tests/fixtures/bestplan_eval_sampling_frame.example.sha256 \
  --design-manifest tests/fixtures/bestplan_eval_design.example.json \
  --design-hash tests/fixtures/bestplan_eval_design.example.sha256 \
  --cost-bound-manifest tests/fixtures/bestplan_eval_cost_bounds.example.json \
  --cost-bound-hash tests/fixtures/bestplan_eval_cost_bounds.example.sha256 \
  --design factorial_2x2 \
  --phase pilot \
  --attempt-count 5 \
  --seed 20260724 \
  --fixture-only \
  --dry-run

python scripts/evaluate_bestplan.py prepare-sources \
  --cases tests/fixtures/bestplan_eval_cases.example.jsonl \
  --condition-map /tmp/bestplan-eval-condition-map.json \
  --workspace-corpus tests/fixtures/bestplan_eval_workspaces \
  --source-input tests/fixtures/bestplan_eval_source_input.example.json \
  --source-map /tmp/bestplan-eval-source-map.json \
  --source-corpus /tmp/bestplan-eval-source-corpus.json

python scripts/evaluate_bestplan.py run \
  --cases tests/fixtures/bestplan_eval_cases.example.jsonl \
  --output /tmp/bestplan-eval-dry-run.jsonl \
  --condition-map /tmp/bestplan-eval-condition-map.json \
  --runtime-config tests/fixtures/bestplan_eval_runtime.example.yaml \
  --design-manifest tests/fixtures/bestplan_eval_design.example.json \
  --design-hash tests/fixtures/bestplan_eval_design.example.sha256 \
  --cost-bound-manifest tests/fixtures/bestplan_eval_cost_bounds.example.json \
  --cost-bound-hash tests/fixtures/bestplan_eval_cost_bounds.example.sha256 \
  --source-map /tmp/bestplan-eval-source-map.json \
  --source-corpus /tmp/bestplan-eval-source-corpus.json \
  --phase pilot \
  --seed 20260724 \
  --attempt-count 5 \
  --max-dispatches 28 \
  --max-cost-usd 10 \
  --worst-case-run-cost-usd 0.25 \
  --scoring-budget-records 1000 \
  --scoring-budget-hours 10 \
  --fixture-only \
  --dry-run
```

Expected: schema/budget summary, zero model calls, and no live-state writes.
`--fixture-only` is a compiled test path that permits the known one-family
fixture and dirty implementation tree only when both init and run are
`--dry-run`; it rejects operator approval, a real runner, sizing, or any model
dispatch. Real pilot/confirmatory flows always require the exact clean commit.

- [ ] **Step 12: Run only the synthetic lens-ablation dry run**

Run the same `run-lens-ablation` command from Step 9 against the synthetic
case fixture and a checked-in synthetic positive summary, with `/tmp` output
and condition-map paths plus derived `--max-dispatches 42`, fixture sampling/
design/cost-bound hashes, scoring budgets, `--fixture-only`, and `--dry-run`. First run
fixture-only dry-run `init --design lens_ablation` for that distinct
map/evaluation ID, then `prepare-sources` against it; do not reuse the
factorial source artifacts.

Expected: six-condition schema/budget summary, zero model calls, no
live-state writes, and no production config surface.

- [ ] **Step 13: Validate the synthetic judgments**

Run:

```bash
python scripts/evaluate_bestplan.py export-phase-a \
  --cases tests/fixtures/bestplan_eval_cases.example.jsonl \
  --results tests/fixtures/bestplan_eval_results.example.jsonl \
  --bundle /tmp/bestplan-phase-a-bundle.jsonl

cmp /tmp/bestplan-phase-a-bundle.jsonl \
  tests/fixtures/bestplan_eval_phase_a_bundle.example.jsonl

python scripts/evaluate_bestplan.py validate-phase-a \
  --bundle tests/fixtures/bestplan_eval_phase_a_bundle.example.jsonl \
  --judgments tests/fixtures/bestplan_eval_phase_a_judgments.example.jsonl \
  --consensus /tmp/bestplan-phase-a-consensus.jsonl \
  --raw-manifest-hash /tmp/bestplan-phase-a-judgments.sha256 \
  --consensus-hash /tmp/bestplan-phase-a-consensus.sha256

cmp /tmp/bestplan-phase-a-consensus.jsonl \
  tests/fixtures/bestplan_eval_phase_a_consensus.example.jsonl

python scripts/evaluate_bestplan.py export-phase-b \
  --results tests/fixtures/bestplan_eval_results.example.jsonl \
  --phase-a-consensus \
    tests/fixtures/bestplan_eval_phase_a_consensus.example.jsonl \
  --phase-a-hash /tmp/bestplan-phase-a-consensus.sha256 \
  --phase-a-raw-manifest-hash /tmp/bestplan-phase-a-judgments.sha256 \
  --bundle /tmp/bestplan-phase-b-bundle.jsonl

cmp /tmp/bestplan-phase-b-bundle.jsonl \
  tests/fixtures/bestplan_eval_phase_b_bundle.example.jsonl

python scripts/evaluate_bestplan.py validate-phase-b \
  --cases tests/fixtures/bestplan_eval_cases.example.jsonl \
  --bundle tests/fixtures/bestplan_eval_phase_b_bundle.example.jsonl \
  --judgments tests/fixtures/bestplan_eval_phase_b_judgments.example.jsonl \
  --consensus /tmp/bestplan-phase-b-consensus.jsonl \
  --raw-manifest-hash /tmp/bestplan-phase-b-judgments.sha256 \
  --consensus-hash /tmp/bestplan-phase-b-consensus.sha256

cmp /tmp/bestplan-phase-b-consensus.jsonl \
  tests/fixtures/bestplan_eval_phase_b_consensus.example.jsonl
```

Expected: exported bundles byte-match the checked-in examples, both validators
PASS against one complete synthetic blinded four-condition block, and Phase B
is bound to both exact frozen Phase A hashes while its own raw judgments and
derived canonical consensus receive separate frozen hashes that consumers
re-derive.

- [ ] **Step 14: Stage, inspect, and commit**

Run:

```bash
git add scripts/evaluate_bestplan.py \
  scripts/bestplan_eval_checks.py \
  agent/bestplan_orchestrator.py \
  tests/scripts/test_evaluate_bestplan.py \
  tests/agent/test_bestplan_orchestrator.py \
  tests/fixtures/bestplan_eval_cases.example.jsonl \
  tests/fixtures/bestplan_eval_results.example.jsonl \
  tests/fixtures/bestplan_eval_phase_a_bundle.example.jsonl \
  tests/fixtures/bestplan_eval_phase_a_judgments.example.jsonl \
  tests/fixtures/bestplan_eval_phase_a_consensus.example.jsonl \
  tests/fixtures/bestplan_eval_phase_b_bundle.example.jsonl \
  tests/fixtures/bestplan_eval_phase_b_judgments.example.jsonl \
  tests/fixtures/bestplan_eval_phase_b_consensus.example.jsonl \
  tests/fixtures/bestplan_eval_positive_summary.example.json \
  tests/fixtures/bestplan_eval_runtime.example.yaml \
  tests/fixtures/bestplan_eval_sampling_frame.example.json \
  tests/fixtures/bestplan_eval_sampling_frame.example.sha256 \
  tests/fixtures/bestplan_eval_design.example.json \
  tests/fixtures/bestplan_eval_design.example.sha256 \
  tests/fixtures/bestplan_eval_cost_bounds.example.json \
  tests/fixtures/bestplan_eval_cost_bounds.example.sha256 \
  tests/fixtures/bestplan_eval_check_attestation.example.json \
  tests/fixtures/bestplan_eval_source_input.example.json \
  tests/fixtures/bestplan_eval_source_map.example.json \
  tests/fixtures/bestplan_eval_source_corpus.example.json \
  tests/fixtures/bestplan_eval_workspaces/synthetic-001/
git diff --cached
```

Run GitNexus staged `detect_changes`, then commit:

```bash
git commit -m "feat(bestplan): add grounded quality evaluation harness"
```

### Task 9: Document operator-visible behavior

**Files:**

- Modify: `hermes_cli/subcommands/bestplan.py`
- Modify: `tests/hermes_cli/test_bestplan_cli.py`
- Modify: `cli-config.yaml.example`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`
- Modify: `website/docs/user-guide/configuration.md`

- [ ] **Step 1: Pass the graph gate**

Run GitNexus impact for `cmd_bestplan`.

- [ ] **Step 2: Write CLI RED tests**

Prove `hermes bestplan lanes` shows:

- explorer order and named synthesizer;
- `candidate_contract`;
- `lens_contract`;
- validation status; and
- no API key, endpoint, raw evidence, token count, cost, or private path.

- [ ] **Step 3: Run the RED tests**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_bestplan_cli.py -q
```

Expected: FAIL because experiment modes are not displayed.

- [ ] **Step 4: Update CLI and documentation**

Document:

- compact visible output and structured receipt ownership;
- the two independent experiment switches;
- why operational lenses are not static model roles;
- the enforced BestPlan-only child read scope;
- why non-current experiments reject `codex_app_server` until its native
  tools/internal calls have a separately proven containment/accounting path;
- telemetry's reported/partial/unavailable usage and
  provenance-bound actual/estimated/included/unknown cost semantics;
- host-dispatch budgets versus unavailable provider-request counts;
- immutable case snapshots/frozen web adapters;
- two-phase blinded evaluation and frozen Phase A consensus;
- V2 receipt compatibility during evaluation;
- pilot and confirmatory acceptance gates; and
- the separate approval required for paid evaluation or live activation.

Do not claim Kimi K3 cost is zero or known.

- [ ] **Step 5: Run the GREEN tests**

Run the command from Step 3.

Expected: PASS.

- [ ] **Step 6: Stage, inspect, and commit**

Run:

```bash
git add hermes_cli/subcommands/bestplan.py \
  tests/hermes_cli/test_bestplan_cli.py \
  cli-config.yaml.example \
  website/docs/reference/cli-commands.md \
  website/docs/reference/slash-commands.md \
  website/docs/user-guide/configuration.md
git diff --cached
```

Run GitNexus staged `detect_changes`, then commit:

```bash
git commit -m "docs(bestplan): explain grounded quality experiments"
```

### Task 10: Run source acceptance and independent review

**Files:**

- No new files expected

- [ ] **Step 1: Run focused acceptance**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_bestplan_presentation.py \
  tests/agent/test_bestplan_telemetry.py \
  tests/agent/test_usage_provenance.py \
  tests/agent/test_bestplan_candidate.py \
  tests/agent/test_bestplan_orchestrator.py \
  tests/agent/test_conversation_loop_bestplan.py \
  tests/tools/test_bestplan_read_scope.py \
  tests/tools/test_bestplan_evaluation_io.py \
  tests/run_agent/test_codex_app_server_integration.py \
  tests/hermes_cli/test_bestplan_cli.py \
  tests/scripts/test_evaluate_bestplan.py -q
```

Expected: PASS.

- [ ] **Step 2: Run the full supported suite**

Run:

```bash
scripts/run_tests.sh -q
```

Expected: PASS. Report exact passed/failed/skipped counts and duration.

- [ ] **Step 3: Prove receipt compatibility**

Run the focused receipt fixture tests and inspect one generated V2 success and
failure record. Confirm:

- V1 and V2 readers still validate;
- new writes remain V2 during the experiment;
- parsed marker JSON equals durable JSONL;
- compact visible output has no marker; and
- no telemetry/evidence raw data entered V2.

- [ ] **Step 4: Prove secret and identity boundaries**

Run all sentinel-secret, malicious-ledger-token, path traversal, late-worker
tombstone, immutable-snapshot, frozen-web/no-live-fallback,
experimental-app-server-rejection, identity drift, K3 endpoint/mode,
provider-error, timeout, and receipt-persistence tests.

Expected: PASS with no sentinel in captured output or logs.

- [ ] **Step 5: Run the graph acceptance gate**

Stage any final test/doc corrections, run GitNexus staged `detect_changes`, and
then run compare mode against default branch `main` as required by the Hermes
Agent repository. Also inspect the exact diff from the pinned K3 base commit
`a62e8c2c1ba53827a7d3df88efa547dd85bf97dd` so this plan's additions can be
separated from the already-reviewed generic-pool/K3 work. Confirm only the
three governing BestPlan changes, their CLI/docs, and the isolated evaluator
are affected.

- [ ] **Step 6: Request independent code and specification review**

Invoke `@superpowers:requesting-code-review` with:

- all three governing 2026-07-24 specs: compact output, grounded exploration,
  and generic explorer-pool/Kimi K3;
- this implementation plan;
- exact base/head commits;
- focused and full-suite receipts; and
- explicit review prompts for secret leakage, receipt compatibility, identity
  truthfulness, equal-budget experiment design, and static-role creep.

Fix all valid findings and rerun the relevant gates.

Before dispatching review, assert the packet contains all three exact spec
paths from the Governing specifications section; a missing contract fails the
acceptance step.

- [ ] **Step 7: Verify the worktree is clean**

Run:

```bash
git status --short --branch
git log --oneline --decorate -12
```

Expected: clean `codex/bestplan-quality` branch with intentional, reviewable
commits only.

## Post-source evaluation checkpoint

Do not run this checkpoint as part of source implementation.

1. Rotate the exposed Kimi credential and validate the replacement through the
   normal secret path.
2. Freeze the clean-code experiment design, target population/category
   weights, RNG/calibration contract, consumed-family ledger snapshot, and one
   atomic mutually disjoint allocation for the 20-family factorial pilot/
   holdout and 20-family lens pilot/holdout before any factorial output.
   Every case has one family representative, observation cutoff, provenance,
   content-addressed snapshot, safe sources, and frozen web/task corpus. Use
   only host-mediated runtimes; experimental `codex_app_server` preflight must
   fail.
3. Run evaluator `--dry-run` and review exact host dispatches, tariff-derived
   hard-cost reservation, worst-case/expected judgment records, and scorer
   hours.
4. Obtain explicit approval for real model calls.
5. Run the one-seed, count-five 2-by-2 pilot in isolated state so all lenses
   participate.
6. Stop on leakage, invalid-candidate regression, quorum regression, or budget
   breach.
7. Export pilot Phase A, obtain two hidden primary scores per subjective
   artifact, adjudicate every field-level disagreement, verify executable
   attestations, and freeze raw-manifest plus canonical-consensus hashes.
8. Duplicate-score/adjudicate Phase B Stage B1 and the complete controlling B2
   census, freeze its raw and consensus hashes, then run deterministic
   weighted/family-stratified sizing and exact calibration. Freeze the maximum
   family count across every gate, including critical-miss non-inferiority,
   semantic grounding floors, and the alpha-split lens prerequisite.
9. Obtain approval for the exact dispatch, tariff-derived cost, judgment-
   record, and scorer-hour budget. If any denominator, stratum, calibration,
   or budget cannot cover the frozen size, report `inconclusive`.
10. Atomically reserve/select only the first frozen `N` unused holdout
    families, run three-seed confirmation under a fresh ID/map, duplicate-
    score both phases, and apply the target-population-weighted gates.
11. If combined minus evidence-only passes the alpha-split quality rule plus
    package p95-latency/cost/coverage guardrails, obtain separate approval and
    run the already pre-frozen count-five six-condition lens pilot; then freeze
    a multiple-of-five unused-family size with `.80` joint all-five power
    under both registered pathways.
12. Reserve/select the pre-frozen lens holdout, run and blindly duplicate-
    score the confirmatory full schedule plus five leave-one-lens-out
    schedules, and promote only if every lens has measurable unique quality
    contribution and the package-level efficiency prerequisite remains valid.

## Conditional promotion plan

Only after the confirmatory report passes:

- mark `candidate_contract: evidence_v2` recommended only for the exact
  validated runtime/treatment fingerprint and target task population,
  inclusion rules, and strata;
- mark `lens_contract: operational` recommended only if it beats
  evidence-only within the package efficiency caps and the separately
  budgeted lens ablation shows measurable unique contribution under that same
  fingerprint/population;
- keep the global defaults at `v1/current` while the compiled pool contains
  `codex_app_server`; changing those defaults requires a separate proven
  app-server containment/accounting design or a separately approved default
  runtime change;
- add a checked-in receipt V2 fixture;
- design and implement exact receipt V3 markers and validators containing only
  candidate digests/counts, admission aggregates, and allowlisted telemetry;
- retain permanent V1/V2 reading and mixed-version reconciliation; and
- repeat the full source, secret, receipt, and isolated live acceptance gates.

If a condition does not pass, keep it disabled and remove dead experimental
surface rather than carrying an unproven permanent feature.
