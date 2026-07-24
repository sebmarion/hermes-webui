# BestPlan Grounded Exploration and Measurement Design

**Date:** 2026-07-24

## Goal

Improve BestPlan's factual grounding and explorer diversity without assuming
that extra structure or role labels automatically improve plan quality.

This design covers three proposed changes:

1. evidence-bearing explorer candidates;
2. operationally differentiated explorer lenses; and
3. per-attempt latency, token, and qualified cost telemetry.

It complements the existing
`2026-07-24-bestplan-compact-output-design.md`. The compact model ledger and
plan body remain the user-visible format; the quality and telemetry changes
below remain host-owned and structured.

## Evidence verdict

### Evidence-bearing candidates: build experimentally

There is good evidence for structured, verification-aware handoffs, but not a
direct study of this exact BestPlan design.

- VeriMAP combines structured named I/O with planner-defined verification
  criteria and outperforms single- and multi-agent baselines, especially on
  harder coding and mathematics tasks.
- ReAct shows that external observations can reduce hallucination and error
  propagation compared with unsupported reasoning.
- ALCE shows the limit of citation-shaped output: even strong systems often
  lack complete citation support. A locator must therefore be resolved by the
  host; the model may not declare its own evidence verified.

Decision: implement a strict evidence candidate contract behind an experiment
switch. Promote it only if it reduces unsupported critical claims and improves
verified plan success at a matched budget.

### Static explorer personas: do not build

The current orchestrator already rotates five strategies:
`evidence-first`, `counterfactual`, `failure-first`,
`verification-first`, and `scope-first`.

Research does not justify binding labels such as "architect" or "skeptic" to a
specific configured model:

- a large EMNLP study found that persona prompts did not improve factual
  performance over no-persona controls;
- learned role differentiation has produced gains in some settings and
  regressions in others; and
- equal-budget multi-agent debate can underperform simple independent sampling
  and voting. Separate work shows anonymization can reduce identity-driven
  sycophancy without necessarily improving overall accuracy.

Decision: strengthen the existing strategies into operational lenses with
different questions and required artifacts. Keep lens assignment independent
of explorer identity so the SOTA model array remains replaceable and
reorderable. Test lenses separately from the evidence contract.

### Telemetry: build, but do not claim it improves plans

Telemetry is an observability feature. It does not make a plan better by
itself. It makes the evidence and lens experiments measurable and lets Hermes
compare quality per unit of latency and cost.

The existing child result supplies normalized token and cost fields, but its
multi-call completeness/provenance is insufficient. BestPlan can add a
host-owned per-turn ledger at the canonical call seams without changing
provider adapters or pricing behavior.

## Current constraints

- `bestplan.explorers` is a dynamic ordered array of one to five model entries.
- Effective attempts remain two to five and cycle across that array.
- The named synthesizer remains explicit.
- Host-mediated explorer children use only read/search and web inspection.
  The current `codex_app_server` runtime is not equivalent: it owns native
  command/file tools, derives its own working directory, and may enable
  internal multi-agent work for Ultra.
- Quorum remains based on structurally valid candidate packets, not model
  identity, evidence count, or self-reported confidence.
- Provider resolution, Kimi K3 trust checks, timeouts, and teardown remain
  independent of quality experiments.
- Candidate packets and their evidence text are untrusted model output.
- Receipt version 2 uses exact key sets and cannot accept silent extensions.

Before any of this work, the checked-in Agent default must be reconciled:
`hermes_cli.config.DEFAULT_CONFIG["bestplan"]` currently names synthesizer
`strongest`, while canonical validation requires the name of a configured
explorer. The default must use `sol`, and a regression test must validate the
checked-in default through the real normalizer.

## Enforced child read scope

The pinned implementation is not yet workspace-confined:
`_run_child_agent()` omits the parent task ID, `read_file` accepts absolute
paths, and `codex_app_server` bypasses the Hermes tool registry entirely.
Grounded evidence must not build on that false boundary.

Before any child is created, the host resolves one immutable
`BestPlanReadScope` from the parent turn's effective task ID and authoritative
workspace root. Failure to establish the root fails closed before dispatch.
Each explorer and synthesizer receives a unique derived task ID registered to
that exact root, and `_run_child_agent()` passes it to
`run_conversation(..., task_id=...)`.

Eligible host-mediated runtimes are `chat_completions`, `anthropic_messages`,
`bedrock_converse`, and `codex_responses` only when its resolved transport
parameters prove that it will preserve Hermes client-side tools. In
particular, resolved xAI Responses sessions (`is_xai_responses: true`) are
ineligible: the pinned transport replaces the Hermes `web_search` function
with provider-native search, which bypasses Hermes tool results and citation
plumbing. Experimental preflight rejects that resolved variant; a generic
mode label is not sufficient proof of containment. For eligible registered
BestPlan task IDs only, `read_file` and every `search_files` entry point
enforce the scope after normalization and realpath resolution:

- relative and absolute paths must remain under the immutable root;
- `..`, symlink, mount, and sibling-worktree escapes are rejected;
- search roots and every returned path are checked;
- concurrent BestPlan runs keep separate registries.

Every derived ID uses a reserved, non-reusable BestPlan prefix. A prefixed ID
without an active registry entry always fails closed instead of falling back
to ordinary tool behavior. On completion the active scope is removed; on
timeout it is first made inactive, so a quarantined late worker is denied
even if teardown cannot prove the worker exited. Ordinary non-BestPlan task
IDs retain existing behavior.

Admission uses the same immutable scope object rather than independently
guessing a root. This is a scoped read guard, not a global change to ordinary
Hermes file-tool behavior.

`codex_app_server` is explicitly unsupported for `evidence_v2`, operational
lenses, and every evaluation condition until a separate implementation proves
native command/file/network confinement and internal-call accounting. Strict
preflight rejects an experimental run if any scheduled explorer or the named
synthesizer uses that mode. Compact V1 output and telemetry may continue to
observe the existing app-server path, but they must label its internal model
turn count opaque. An isolated experiment may use the same model through a
host-mediated API mode if that route independently validates; it may not
silently rewrite a configured runtime.

## Deterministic evaluation inspection

Real evaluation never reads the evaluator's current checkout. Each case
references a redacted, content-addressed workspace snapshot plus an exact
manifest of relative file paths, sizes, and SHA-256 values. The runner
verifies and extracts that snapshot to an isolated read-only root before
dispatch, then binds every condition for that case to the same root.

A private typed `BestPlanEvaluationIO` dependency carries that root, the
visible task-fact registry, and a frozen web-search/fetch corpus. For
evaluation-prefixed task IDs, the Hermes file and web handlers consult this
dependency:

- file reads/searches are served only from the verified snapshot root;
- task facts resolve only by issued fact ID;
- web search returns only frozen results for an exact registered query;
- web extract returns only the frozen body for an exact registered canonical
  URL; and
- missing adapters, unknown calls, live-network fallbacks, browser tools, and
  expired task IDs fail closed.

The host derives and registers the child task ID before constructing its
agent. `_build_child_agent(task_id=...)` passes it through
`AIAgent.__init__`, `init_agent`, and
`model_tools.get_tool_definitions(task_id=...)`; the later `run_conversation`
call must use the same immutable ID. The adapter is therefore present before
child tool-definition assembly. For an evaluation ID, tool-definition assembly
bypasses the mutable registry entirely and returns exact host-owned canonical
schemas for the closed frozen file/search/web handlers. It neither invokes
plugin handler/schema/check callbacks nor applies dynamic schema overrides.
Evaluation-aware web capability checks take the derived task ID: they expose
`web_search`/`web_extract` from the frozen corpus even when no live provider is
configured, and expose neither when the matching frozen capability is absent.
`web_capability_fingerprint` includes the evaluation-adapter generation and
corpus digest so the global/live-provider memoization cannot hide a newly
registered adapter or retain an expired one. Ordinary tasks keep the existing
global availability behavior. Tests cover the complete tool-definition path,
not only direct handler calls.

The registry's existing 30-second callable-only availability cache is never
consulted for evaluation definitions. Evaluation schema caching, if any, keys
by `(task_id, adapter_generation, corpus_digest)` and invalidates on expiry.
Sequential evaluation-A, ordinary, evaluation-B, and expired-A construction
must not leak a capability result across tasks. Ordinary construction retains
the existing registry, plugin overrides, dynamic schema behavior, and TTL
cache.

For an evaluation-prefixed task ID, `model_tools.handle_function_call` uses a
closed direct dispatcher for the approved frozen file/search/web handlers.
Global tool-request middleware and plugin pre/post/result-transform hooks are
not invoked: they can rewrite a query/path, replace a frozen result, persist
source data, or perform network side effects. Any tool outside the closed
evaluation allowlist fails closed. Registry handler replacements are also
ignored because the dispatcher holds exact canonical built-in references.
Ordinary tasks keep their current registry/middleware/plugin behavior.
End-to-end tests install malicious registry schema/check/handler overrides,
dynamic schemas, rewrite/transform/post hooks, and prove none runs or appears
for an evaluation task.

The adapter is an in-process evaluation dependency, never configuration or a
production tool mode. Every four- or six-arm block records the same snapshot
and source-corpus digests. App-server runtimes cannot consume this adapter,
and xAI Responses can replace its client search tool after registry
construction; both are therefore rejected rather than treated as equivalent.

## Experiment controls

Add two independent, strict BestPlan settings:

```yaml
bestplan:
  candidate_contract: v1          # v1 | evidence_v2
  lens_contract: current          # current | operational
```

During evaluation both default to the current behavior. This creates a
budget-matched 2-by-2 comparison:

| Candidate contract | Lens contract | Condition |
|---|---|---|
| `v1` | `current` | baseline |
| `evidence_v2` | `current` | evidence only |
| `v1` | `operational` | lenses only |
| `evidence_v2` | `operational` | combined |

Unknown values fail validation before dispatch. The switches do not change the
model pool, count, timeouts, tools, or synthesizer. Test and evaluation code may
override them in an isolated runtime config; no live Hermes configuration is
changed by source implementation.

Before sampling-frame allocation or pilot dispatch, an owner-only
`freeze-design` command emits one canonical `experiment_design_manifest` and
its SHA-256; pilot `init` consumes them and never creates or mutates them. The
manifest binds:

- the exact clean source commit;
- Candidate V1/V2 schema and parser versions;
- current and operational prompt/lens schedule hashes;
- the anonymous synthesis-envelope version;
- the closed evaluation tool schemas and dispatcher version;
- the executable-check registry and runner version;
- evidence-admission, telemetry-projection, and execution-projection versions;
- evaluator metric, sizing, and bootstrap implementation versions;
- the qualified tariff/contract and derived hard-cost-bound manifest digest;
- the target task population, inclusion/exclusion rules, primary category
  weights, difficulty-balancing rule, hypothesis-family ID, and consumed-
  family-ledger snapshot; and
- the full resolved runtime fingerprint: explorer pool and order with resolved
  model versions, attempt count, synthesizer, runtime modes, tool set, and
  stage/token/timeout/dispatch limits.

Pilot init, every result, the protected source corpus, both judgment bundles,
the sizing report, fresh confirmatory init, and the final summary carry and
hash-verify this design digest. Resume or confirmation fails before dispatch
if the checkout is dirty, a component hash changes, or any runtime dimension
differs. A source or prompt change requires a new pilot; it cannot be treated
as the same experiment. Promotion is scoped to this full fingerprint and the
frozen target task population/inclusion rules/strata, not merely to a
model-pool/order label or a narrow case sample.

## Evidence candidate contract

`evidence_v2` explorers return exactly one JSON object after the exact marker
`HERMES_BESTPLAN_CANDIDATE_V2`. No prose, Markdown fence, prefix text, second
JSON object, or trailing text is accepted.

The logical shape is:

```json
{
  "schema": "HERMES_BESTPLAN_CANDIDATE_V2",
  "summary": "Concise recommendation",
  "steps": ["Bounded implementation step"],
  "risks": ["Risk with trigger and consequence"],
  "verification": ["Executable or observable acceptance check"],
  "evidence": [
    {
      "claim": "steps[0]",
      "kind": "file",
      "locator": {
        "type": "file_line",
        "path": "agent/bestplan_orchestrator.py",
        "line_start": 755,
        "line_end": 755
      },
      "support": "direct",
      "note": "The orchestrator schedules attempts here."
    }
  ],
  "assumptions": ["Explicitly unverified premise"],
  "unknowns": ["Information that could not be established"]
}
```

Validation is host-owned:

- the top-level and evidence-object key sets are exact;
- `summary` is a non-empty bounded string;
- `steps` and `verification` contain 1 to 12 bounded strings;
- `risks`, `assumptions`, and `unknowns` contain 0 to 12 bounded strings;
- `evidence` contains 0 to 32 bounded objects;
- at least one of `evidence` or `unknowns` is non-empty, so honest "not found"
  is valid and fabricated evidence is not required;
- `claim` is `summary`, `steps[n]`, `risks[n]`, or `verification[n]`, and every
  referenced index exists;
- `kind` is `task`, `file`, or `web`;
- `locator` is an exact kind-specific object:
  - file: `{type: file_line, path: <workspace-relative POSIX path>,
    line_start: <int>, line_end: <int>}`;
  - task: `{type: task_fact, fact_id: <host-issued visible fact ID>}`; or
  - web: `{type: web_source, url: <exact canonical fetched/opened URL>}`;
- absolute file paths, unissued task facts, unissued web observations, and a
  web search query without a fetched/opened source observation are invalid or
  unresolved, never admitted;
- `support` is `direct`, `indirect`, or `counterevidence`;
- control characters and oversized packets are rejected before synthesis; and
- all V2 strings and the raw packet have explicit byte/character bounds.

There is no candidate confidence field. Model self-confidence is not used for
quorum or synthesis weighting.

The explorer prompt includes a bounded registry of host-issued IDs for facts
explicitly visible in the user task. Web evidence must name an exact canonical
URL that appears in a successful fetched/opened source result; search-result
lists and model-chosen query strings do not count. During evaluation, all four
arms use the same frozen read-only task/source adapter, and hidden gold facts
are never issued to explorers.

Candidate V1 remains available only in the `candidate_contract: v1`
experiment arm. It is never counted as an evidence V2 success.

## Host evidence admission

The child does not provide a verification status. After structural validation,
the host creates an admission envelope for each evidence item:

```json
{
  "claim": "steps[0]",
  "kind": "file",
  "locator_status": "resolved",
  "admission_provenance_sha256": "host-owned-hex-or-null"
}
```

`locator_status` is one of:

- `resolved`: a bounded file locator resolves under the allowed workspace and
  the cited line exists;
- `observed`: a task fact ID exists in the host-issued visible-fact registry,
  or a canonical web URL matches an exact successful fetched/opened source
  result in the child's host registry; or
- `unresolved`: the host cannot establish the locator from permitted data.

The status proves locator provenance, not semantic entailment. The
synthesizer is told that candidate claims, notes, and source interpretation
remain untrusted. It must prefer resolved or observed support, preserve
counterevidence, and surface material unresolved assumptions.

For every `resolved` or `observed` item, the host computes a non-null opaque
admission provenance digest from the authoritative surface for that kind:

- file provenance comes from the admission-time bounded reread under
  `BestPlanReadScope` and hashes the evidence kind, admitted source-byte digest,
  and canonical bounded locator metadata;
- task provenance hashes the exact host-issued visible fact record without a
  tool call; and
- web provenance requires exact call/result matching and hashes the canonical
  fetched URL plus matched successful result-byte digest.

Only web uses the volatile call ID to prove call/result association, and that
ID is not part of the stable digest. `unresolved` items have null provenance.
Null or malformed provenance on resolved/observed evidence fails admission;
one `(kind, provenance)` key cannot map to conflicting safe sources.

Model-chosen queries and dynamic locator strings never identify sources. Case
preparation builds an owner-only source map from the same frozen visible-fact
and canonical-URL corpus used by the scoped adapter. It also creates approved
file-source range records from the immutable snapshot:

```json
{
  "case_source_id": "source-file-001",
  "path": "agent/bestplan_orchestrator.py",
  "line_start": 730,
  "line_end": 790,
  "source_bytes_sha256": "owner-only-hex"
}
```

At admission, a file citation maps to a safe source only when its normalized
path and full cited interval are contained in exactly one approved range and
the frozen bytes still match. An exact range and an unambiguous subset map to
that safe ID; a superset, cross-boundary range, zero-match range, or ambiguous
overlap is unmapped and fails closed for evidence scoring. Task/web
provenance remains exact and reproducible before dispatch.

A model-supplied digest is never trusted. The private evaluation sink may
receive only the already resolved safe `case_source_id`, evidence kind, and
locator status. The host keeps `admission_provenance_sha256` private and uses
the owner-only map while the raw locator is still available; the digest is
removed from synthesis packets and never written to the evaluation sink,
scorer-visible output, BestPlan receipts, general telemetry, or visible text.
Raw source excerpts, evidence notes, paths, URLs, call IDs, and tool results
remain private.

## Operational lenses

`lens_contract: operational` keeps the existing strategy names and scheduling
order but replaces one-line labels with host-owned checklists:

- `evidence-first`: establish canonical facts, source locators, assumptions,
  and freshness gaps;
- `counterfactual`: identify plausible alternatives, falsifiers, and evidence
  that would reverse the recommendation;
- `failure-first`: provide severity, trigger, blast radius, containment, and
  rollback hazards;
- `verification-first`: provide executable acceptance checks, expected
  signals, negative tests, and rollback proof; and
- `scope-first`: map contracts, dependencies, sequencing, non-goals, and the
  smallest safe change.

These are work contracts, not personas. They do not change the child toolset
or grant a model authority over a subsystem.

Candidates are independent. Before synthesis, the host removes configured and
resolved model identity from candidate packets. The synthesizer receives only
attempt index, operational lens, admitted evidence metadata, and candidate
content. The visible response still shows the truthful host-owned model ledger
after synthesis.

That bounded anonymous envelope is an explicit experimental-contract boundary.
Default production `v1/current` with no evaluation dependencies preserves the
pinned legacy synthesis packet exactly. Every non-current contract and every
evaluation arm—including the `v1/current` evaluation baseline—uses the same
named envelope; only the candidate/lens factors differ. Attaching the private
evaluation sink never selects or mutates a packet. The 2-by-2 result therefore
estimates evidence/lens effects within the common-envelope regime, and any
promotion of `evidence_v2` includes that envelope. It does not claim the
evaluation baseline is byte-identical to an ordinary legacy production turn.

The synthesizer must preserve a supported minority finding when it conflicts
with an unsupported majority. It must not vote by candidate count.

## Runtime telemetry

Capture telemetry at `_run_child_agent`, where the full `run_conversation`
result is currently reduced to `final_response`.

Separately, `run_bestplan()` records one end-to-end monotonic wall-clock
interval. It starts immediately after successful runtime/evaluation admission,
before the first explorer is submitted, and ends when the host creates the
terminal result after quorum/synthesis/reformat/failure handling. Concurrent
child times are not summed. An admitted timeout ends at host terminal
classification; a preflight failure that never admits the condition has null
total latency and cannot enter an evaluation block. This exact
`total_latency_ms` is the sole controlling p95 latency input.

Hermes currently initializes token counters to zero and returns those integers
even when a provider supplied no usage object. Track per-call usage provenance
at both canonical accounting surfaces:

- the normal provider-response accounting branch in
  `agent/conversation_loop.py`; and
- the Codex app-server terminal-turn seam,
  `agent/codex_runtime.py::_record_codex_app_server_usage`.

The same Codex helper is also used for an auxiliary compaction request.
Compaction accounting must not be mistaken for terminal-turn provenance:
either the helper returns an explicit usage-presence fact for its caller to
classify, or it takes an explicit call-kind argument. Conversely, a
max-iteration summary, its empty-response retry, and any other model-driving
call that contributes to the returned child result must participate in usage
coverage even though the legacy `api_calls` field counts only main-loop
iterations. A runtime whose internal calls cannot be enumerated may not report
complete coverage merely because its outer turn returned usage.

Classify each finalized API response as:

- `complete`: the provider object contains recognized input/prompt and
  output/completion fields that canonical normalization can represent,
  including a genuine all-zero report; total may be checked or derived;
- `partial`: at least one recognized core field exists but the complete
  canonical input/output pair cannot be established; or
- `none`: no recognized usage fields are present.

A merely non-empty object is not complete. Optional cache/reasoning buckets
may truthfully default to zero only after the core pair is established.

Reset host-owned complete/partial counters and the opaque-call flag at every
turn boundary.
The finalized `usage_reported` bit is true only when every model-driving call
contributing to the result is known and `complete`; also return
`usage_coverage: complete|partial|none`. A mixed or opaque turn is partial.
Zero-initialized counters and the legacy `api_calls` integer never establish
provenance.

For each dispatched explorer call, project only:

```json
{
  "latency_ms": 1234,
  "host_dispatches": 1,
  "usage": {
    "status": "reported",
    "input_tokens": 1200,
    "output_tokens": 340,
    "total_tokens": 1540
  },
  "cost": {
    "amount_usd": 0.00421,
    "coverage": "complete",
    "status": "estimated",
    "sources": ["official_docs_snapshot"],
    "provenances": ["usage_derived"]
  }
}
```

Rules:

- latency uses host monotonic time from dispatch to terminal classification;
- latency is `null` for attempts that never dispatch;
- `host_dispatches` is exactly one for a dispatched child and zero for a
  construction/preflight failure; it is not a provider-request count;
- the legacy child `api_calls` value and opaque app-server/subagent activity
  are not projected as provider-request telemetry;
- usage is `reported` only for complete canonical coverage, `partial` when
  some recognized usage exists but the aggregate is incomplete, and
  `unavailable` when none exists; partial/unavailable token aggregates are
  null;
- missing usage or cost is never represented as a truthful-looking zero;
- usage-derived estimated cost is known only when usage coverage is complete;
  otherwise it becomes `unknown` with a null amount;
- provider-reported actual cost and contractually included cost may survive
  partial/no token usage only with independent host-validated provenance;
- cost coverage is `complete` only when every opened dispatch is covered by a
  qualified per-dispatch amount or one authoritative whole-turn amount,
  `partial` when a known amount covers only some activity, and `none` when no
  amount is qualified;
- only `complete` cost coverage carries an amount, status, sorted unique
  allowlisted sources, and provenances; `partial` and `none` expose a null
  amount, `unknown`, and empty arrays rather than a misleading subtotal;
- cost provenance is exactly `usage_derived`, `provider_reported`,
  or `billing_contract`;
- subscription-included, estimated, and unknown cost remain distinct;
- each child is priced at its own provider/model identity;
- Kimi K3 is never labeled free or included without a billing contract; and
- runtime dictionaries, raw responses, messages, errors, endpoints, headers,
  pricing URLs, and secrets never cross the telemetry projector.

The existing session cost fields are not sufficient provenance: the pinned
loop adds amounts across calls but overwrites status and source with the last
call. Each BestPlan child therefore owns a private per-turn accounting ledger.
Every top-level provider dispatch is opened before the call and closed with
that dispatch's usage coverage, cost amount/status/source/provenance, or an
explicit unknown terminal record. Conversation-loop calls, Codex runtime
calls, the max-iteration summary call, and its retry all use the same ledger.
A failed, cancelled, or uninstrumented dispatch makes aggregate usage/cost
incomplete unless an independent provider-reported amount or billing contract
covers it. Legacy cumulative amount plus last-call status/source is never the
telemetry input.

The ledger derives aggregate fields only after all opened dispatches close.
Complete usage requires complete compatible counters on every chargeable
dispatch. Usage-derived cost requires that same completeness and a qualified
price for every dispatch. Mixed `actual`/`estimated`/`included`/unknown calls
retain per-call provenance and follow the aggregate rules below; no later
known call can erase an earlier unknown call. Every `included` record must be
contract-backed and have amount zero. App-server internal activity is
explicitly opaque even when its outer call exposes partial counters.

Synthesis can make one normal call plus one bounded compact-body reformat call.
Each dispatch uses a fresh synthesizer child (same named model/runtime) and is
independently stopped. The reformat prompt contains the complete bounded
context it needs. Reusing one child would expose cumulative session
token/cost counters and double-count the first dispatch. Telemetry therefore
represents synthesis as:

```json
{
  "calls": [
    {
      "kind": "synthesis",
      "latency_ms": 1234,
      "host_dispatches": 1,
      "usage": {
        "status": "reported",
        "input_tokens": 1200,
        "output_tokens": 340,
        "total_tokens": 1540
      },
      "cost": {
        "amount_usd": 0.00421,
        "coverage": "complete",
        "status": "estimated",
        "sources": ["official_docs_snapshot"],
        "provenances": ["usage_derived"]
      }
    }
  ],
  "aggregate": {
    "latency_ms": 1234,
    "host_dispatches": 1,
    "usage": {
      "status": "reported",
      "input_tokens": 1200,
      "output_tokens": 340,
      "total_tokens": 1540
    },
    "cost": {
      "amount_usd": 0.00421,
      "coverage": "complete",
      "status": "estimated",
      "sources": ["official_docs_snapshot"],
      "provenances": ["usage_derived"]
    }
  }
}
```

The calls array contains one `synthesis` entry and, when attempted, one
`reformat` entry in dispatch order. Aggregate latency spans the complete
synthesis stage. Host dispatches are summed exactly. Token buckets are summed
only when every dispatch has complete compatible data; partial dominates
unavailable only when at least one recognized field exists, and aggregate
token values remain null. Cost is summed only when every dispatch has a known
non-cumulative amount with valid independent or complete-usage provenance.
Its complete aggregate status uses precedence
`estimated > actual > included`: all-included is `included`, any estimated
call makes it `estimated`, and otherwise any actual call makes it `actual`.
It is `unknown` when any call is unknown. Aggregate cost coverage is complete only
when every call is complete; otherwise it is partial if any call is known and
none when none is known, with null/unknown/empty public value fields.
Aggregate sources are the sorted unique allowlisted per-call sources, and
aggregate provenances are the sorted unique values from
`usage_derived|provider_reported|billing_contract`.

The compact visible response does not show token or cost details. Telemetry is
returned in structured BestPlan results and consumed by the evaluation
harness. The BestPlan host branch attaches the exact marker-wrapped structured
receipt and bounded telemetry to its finalized result for every
post-validation terminal outcome. It never reconstructs either field from
visible response text.

The compact model ledger remains a ledger of top-level BestPlan explorer and
synthesizer identities resolved by Hermes. It does not invent identities for
opaque app-server internal agents; structured telemetry marks that activity
opaque rather than implying the top-level ledger enumerates every internal
model turn.

## Receipt compatibility

The compact-output and experiment phases keep emitted receipt V2 byte- and
schema-compatible. Telemetry and raw evidence do not enter V2.

If and only if a grounded condition passes the promotion gates, a follow-up
receipt V3 adds exact, bounded structural fields:

- per attempt: operational lens, candidate schema, candidate SHA-256,
  evidence count, admitted-locator counts, and allowlisted telemetry;
- synthesizer: allowlisted telemetry; and
- no raw evidence text, locator, URL, path, exception, or secret.

V1 and V2 marker readers remain permanent. A checked-in V2 fixture is added
before V3 becomes the writer. Mixed receipt versions remain append-only in the
existing `bestplan/receipts.jsonl`.

No telemetry sidecar is introduced in the first implementation. The
evaluation harness owns its isolated result JSONL. This avoids a second
production persistence contract before the experiment proves value.

## Evaluation

### Pilot

Before the factorial pilot dispatches, freeze one owner-only eligible
population/sampling-frame manifest large enough for the pilot plus full
`25..200` confirmatory grid. It contains the complete eligible case IDs,
inclusion/exclusion rules, pre-pilot primary `category` strata and target
weights, within-category difficulty-balancing rules, content-addressed
snapshot/source digests, blinded incident/retry
`case_family_id`, and one deterministic family-stratified split/selection
seed and order. That rule selects the 15/20 pilot representatives first and
the disjoint ordered confirmatory holdout before any output exists; neither
split is handpicked. Related cases stay wholly outside the other split, and
exactly one pre-frozen representative case per family is eligible for any
controlling pilot or confirmation analysis. Thus every grid value `N` means
`N` independent families and `N` dispatched cases; unused relatives may be
descriptive only.

Each case also freezes an `observation_cutoff`, source-commit/archive
provenance, and the task/workspace/web material visible at that cutoff;
accepted resolution and gold facts are strictly post-cutoff and hidden. A
file/web record newer than the cutoff is rejected unless the case manifest
proves it was visible in the original task. The pilot/holdout split digests
are bound into the pilot condition map and experiment-design lineage. The
population cannot be replenished, reordered, re-split, or re-stratified after
pilot dispatch. If it cannot supply a frozen size while preserving the
registered allocation, confirmation is `inconclusive`.

The five named categories are the only controlling bootstrap strata; the
15/20-family pilot therefore supplies respectively three/four independent
families per stratum. Difficulty remains a pre-frozen balance variable and
easy-task subset, not a 15-cell bootstrap stratum. Fewer than three pilot
families in any controlling category makes sizing `inconclusive`.

An owner-only append-only consumed-family ledger is keyed by the registered
BestPlan grounding/lens hypothesis family, not by one source commit. Its
snapshot and digest are inputs to `freeze-design`; frame construction rejects
families previously used or reserved for related pilot/confirmation. Fresh
confirmatory init atomically reserves its selected families before dispatch,
and a reservation remains permanently consumed after any output exists,
unblinding, or ambiguous crash. Every map/result/summary binds the ledger
snapshot and reservation receipt. A new prompt/source variant cannot reuse an
unblinded family as fresh confirmation; it needs unused families or an
independently pre-registered sequential-testing design.

Also before the factorial pilot, freeze the separate lens-ablation eligible
population and deterministic pilot/confirmatory split under the same cutoff,
family, provenance, and one-representative rules. Its digests are added to the
pre-pilot design lineage. This freezes the later ablation target population
without authorizing any ablation dispatch or spend; those remain blocked on
the positive 2-by-2 prerequisite and separate approval. The two frame
allocations are atomic and mutually family-disjoint across both pilot and
holdout sets; any collision fails before either frame is emitted.

Use 15 or 20 redacted historical planning cases across debugging, behavior
changes, deployment, UI, and research so the pilot contains complete
five-block Latin cycles. Run one seed for all four conditions with identical
models, tools, task order, and stage budgets. Set attempt count to five in the
protected condition map and every result so all five strategies participate in
any evidence used to promote operational lenses.

The pilot is a schema, leakage, cost, and gross-regression check. Stop before a
larger run if any condition leaks evidence text, breaks quorum semantics,
increases invalid candidates materially, or exceeds the agreed cost cap.

### Confirmatory run

The pilot is excluded from confirmatory estimates. Sizing waits for the
complete pilot: Phase A duplicate scoring/adjudication and canonical consensus
are frozen, Phase B duplicate scoring/adjudication and canonical consensus are
complete, and both raw/consensus hashes for both phases are frozen. The owner-only
sizing command verifies all four raw/consensus artifacts, then reads the protected
condition map without revealing labels to scorers. It freezes a new-case
confirmatory sample size before any confirmatory dispatch.

The sizing command is deterministic, versioned, and owner-only. It freezes
alpha `.05`, target power `.80`, a recorded RNG seed, 10,000 pilot
family-cluster bootstrap draws, and the candidate new-case grid
`25, 30, ..., 200`. There is no nested bootstrap per simulated dataset.
Pilot family-cluster influence/sandwich variance is used for paired binary
differences, absolute rates, log relative risks, and log mean/cost ratios.
The p95 log-ratio and simultaneous max-spread standard errors/critical values
come from the one 10,000-draw pilot family bootstrap. Because the pilot has
one seed and cannot estimate within-case seed correlation, sizing
conservatively assumes correlation `rho=1`: the three confirmatory seeds earn
no sample-size reduction. Variance scales only by `n_pilot / n`.
Here `n_pilot` and `n` are counts of the one eligible representative case per
independent family, so they are also the dispatched-case counts.
Confirmatory/final bootstraps retain all observed cases, seeds, and conditions
inside each sampled family cluster. Metric-specific one-sided normal power is computed
analytically at every grid value against the exact full decision rule. The
combined-minus-evidence lens prerequisite is the exception: at every grid
value, 100,000 frozen-RNG multivariate-normal draws use the weighted,
within-category pilot covariance of success delta, critical-miss delta, p95
log-latency ratio, log qualified-cost ratio, and qualified-cost coverage. It
must have at least `.80` joint probability of passing the alpha-split quality
branch plus every efficiency/coverage guardrail under both registered
pathways. Singular/non-finite covariance is `inconclusive`. The command
selects the smallest value satisfying every gate, then freezes the maximum:

- verified-success primary contrast: power at a `+10` percentage-point paired
  alternative for the joint rule `point >= +5` and lower bound above zero;
  the replication contrast uses a `+5` point alternative for its lower bound
  above zero;
- unsupported-critical-claim primary contrast: power at a `40%` relative
  reduction for a lower bound of at least the promotion threshold of `25%`;
  the replication contrast uses a `25%` reduction alternative for a lower
  bound above zero;
- critical fact/constraint miss non-inferiority in both primary and
  replication contrasts: power at zero paired change for an upper bound on
  miss-rate increase no greater than `2` percentage points;
- easy-task non-inferiority: power at a zero paired difference for a lower
  bound above `-2` percentage points;
- p95 latency and qualified cost per success: power at a `1.10` ratio for an
  upper ratio bound no greater than `1.25`;
- paired scoreability: power at a `97%` true rate for a lower bound of at
  least `90%`;
- maximum condition unscoreable-rate spread: power at a zero true spread for
  an upper bound no greater than `5` percentage points;
- valid admitted locators and qualified-known-cost coverage: power at a
  `99%` true rate for a lower bound of at least `95%`; and
- correctly classified candidate-evidence semantic relations over the
  complete critical-claim/counterevidence-link census: power at a `97%` true
  rate for a lower bound of at least `90%`, plus descriptive class counts; and
- critical candidate claims with class-consistent evidence or an explicit
  unresolved status: power at a `97%` true rate for a lower bound of at least
  `90%`; and
- supported-or-explicitly-unresolved critical plan claims: power at a `97%`
  true rate for a lower bound of at least `90%`.
- combined-minus-evidence lens prerequisite: power in both a `+5`
  percentage-point success-superiority scenario and a zero-success-delta plus
  `+5` percentage-point critical-miss-reduction scenario under the exact
  alpha-split ordered OR rule, jointly with p95 latency and qualified cost
  ratio `1.10` alternatives against `1.25` upper bounds and 99% true
  qualified-known-cost coverage against its 95% floor.

Final confidence intervals still use the pre-registered 10,000-draw
family-cluster bootstrap. `freeze-design` pre-registers
`cluster_power_calibration_v1`: 20,000 replications per controlling
gate/grid point; `splitmix64_v1` uniform generation with rejection-sampled
indices; `box_muller_v1` normals where required; one fixed calibration seed;
within-category family-vector resampling with replacement that preserves each
`n_h`, target weights, and all correlated estimands; the registered design
alternative added to the centered pilot family influence vector; and no
nested bootstrap. Analytic predicted power is
accepted only if its absolute difference from simulated power is at most
`.03` at every controlling point and the two-sided 95% Monte Carlo binomial
half-width is at most `.01`; otherwise sizing is `inconclusive`. Golden
fixtures freeze the first uniform/index/normal vectors and one complete
calibration result so runtime/library changes cannot silently alter the RNG.

Sizing records this analytic/simulation calibration separately. The frozen record
contains the method version, seed, alternatives, thresholds, variance and
critical-value estimates, pilot empirical cluster-distribution digest,
denominator requirements, experiment-design and pre-pilot sampling-frame
digests, deterministic family/strata allocation, per-gate selected count, and
final maximum. A zero
or undefined baseline for a relative reduction, no verified successes for
cost-per-success, no easy cases, no required locator/claim/cost denominator,
non-finite or unstable variance, failure to reach `.80` power by 200 cases, or
an approved budget below the frozen maximum yields `inconclusive`; it does not
silently substitute a different rule.

Use three seeds and the same explicit attempt count of five, but treat seeds
as repeated measurements within a case, not independent cases.

Confirmation never reuses the pilot map/evaluation ID. After sizing, a fresh
`init --phase confirmatory` receives the immutable pilot map, frozen sizing
report, pre-pilot holdout manifest, exact three-seed list, and same explicit
runtime profile. The requested case file must equal the first frozen `N`
eligible IDs under the registered stratified allocation/order; an arbitrary
new case set is rejected. Init verifies every selected snapshot/source digest,
the holdout sampling-frame digest, pilot lineage, design digest, both
raw/consensus hashes for both phases, and the atomic consumed-family
reservation; excludes every pilot family; requires exactly the frozen
new-family/representative-case count; verifies the same full experiment
fingerprint; and creates a new mode-`0600` confirmatory map/evaluation ID.
That map stores the pilot-map, sizing-report, sampling-frame,
selected-case-set, experiment-design, ledger-snapshot, frame-allocation, and
reservation digests, excluded pilot families, selection seed/order, and
ordered run seeds.
Confirmatory source preparation binds to the fresh ID.
`run --phase confirmatory` and `summarize` both require and hash-verify that
sizing report/map lineage. Missing or changed lineage fails before dispatch or
unblinding.

Blind condition labels during scoring. Each case contains a pre-dispatch
`difficulty` value from the exact enum `easy|standard|hard`, redacted gold
constraints, critical facts, forbidden actions, acceptance checks, a blinded
`case_family_id`, an `observation_cutoff`, source-commit/archive provenance, a
content-addressed workspace snapshot/manifest as visible at that cutoff, and
a bounded evaluation-safe source catalog; it must not contain credentials,
private conversation text, or post-resolution leakage. Difficulty, family,
cutoff, provenance, and snapshot/source digests are frozen into the evaluation
case set before condition IDs or outputs exist. Missing/unknown difficulty or
family, post-cutoff material, cross-split families, and snapshot mismatches
fail before dispatch.

The safe rubric also freezes natural
`direct|indirect|counterevidence` opportunity flags before outputs. They are
used only for class-stratified reporting/applicability and never impose a
quota on a model when that class has no natural opportunity.

Each source-catalog record has an opaque `case_source_id` and exactly one
judgeable surface:

- bounded redacted support text plus its digest; or
- a closed executable-check ID.

A separate owner-only source map connects exact task/web provenance keys and
approved snapshot-backed file range rules to one safe ID. It contains no
model-authored digest and is never given to the scorer. Null, colliding,
missing, ambiguous, or mismatched mappings fail closed. Scorer-visible source
records never contain provenance digests, raw paths, or URLs.

Case preparation computes this map with the same canonical provenance helper
over an owner-only corpus of issued visible task facts and frozen canonical
web URLs/results plus explicit approved file path/line intervals read from the
verified snapshot before model dispatch. The corpus is bound to one evaluation
ID and drives the identical read-only source adapter for all arms. A mutable
task/web observation or file byte range whose canonical result changes no
longer matches and becomes unscoreable; the evaluator never auto-maps it by
semantic similarity. The protected corpus may contain paths, URLs, and source
content, but scorer-visible structured fields, logs, telemetry, and receipts
do not.

Initialization precedes source preparation. An owner-only `init` command first
creates the mode-`0600` condition map with random evaluation/condition IDs and
the case-set digest. `init` requires an explicit isolated runtime-profile path;
it never falls back to live `~/.hermes/config.yaml`. The profile uses the
normal secret-reference resolver but may not contain inline secret values.
Preflight resolves every explorer/synthesizer mode, rejects app-server/xAI
Responses, and freezes a sanitized profile digest plus exact resolved
full experiment fingerprint and design-manifest digest in the map. `prepare-sources
--condition-map ...` then binds the source map and corpus to that exact
evaluation ID. `run` never creates or changes IDs; it requires the map, source
artifacts, explicit runtime profile, digest, and full fingerprint to agree. Resume
and ablation prerequisite checks use the same values.

Scheduling is block-matched. For each seed, derive one deterministic case
order shared by all four conditions. Treat each `(case, seed)` as an atomic
four-run block and randomize only the condition order inside that block. Admit
a block only when the remaining host-dispatch and tariff-derived hard-cost
reservations cover all four runs. A stopped or interrupted partial block is resumed
to completion or excluded in full from paired summaries.

Lens assignment is not fixed to a model position. The protected map freezes a
balanced 5-by-5 Latin-square rotation over the five attempt positions. For a
given `(case, seed)` block, all four conditions use the same row: current
strategy prompts and operational checklists are assigned to the same frozen
positions, while candidate contract is the only other crossed factor. Across
each complete five-block cycle, every strategy/lens appears once at every
model position. Incomplete cycles are excluded as whole cycles from
lens-effect estimates. This removes a fixed lens-to-model-position
confound without changing the ordered model pool.

The deterministic work budget counts host child dispatches, not provider HTTP
requests, internal Codex subagents, or legacy `api_calls`. Those lower-level
counts are not uniformly observable and must never be relabeled as such. With
five explorers and at most two fresh synthesizer dispatches (initial plus one
reformat), the exact ceiling is:

```text
per condition = 5 explorer dispatches + 2 synthesis dispatches = 7
four-condition block = 4 × 7 = 28
six-condition ablation block = 6 × 7 = 42
```

Reserve the full block ceiling atomically before its first call. On completion,
commit the exact host-observed dispatch count and release unused reservation.
Persist reservations so resume completes an admitted block without
double-reserving or admitting a new block prematurely. Paid real-call mode
requires a qualified per-dispatch upper bound derived from frozen maximum
input/output tokens, iteration/retry ceilings, and a versioned provider tariff
or contract. The sum of those bounds for the whole block must fit the approved
cap; a provider with an unknown upper bound is dry-run-only. This is the only
hard spend-cap claim. Observed cost and known-cost coverage are still reported
separately, and the host-dispatch ceiling is never represented as a provider
billing ceiling.

Budget CLI inputs are strict decimal/integer values: no booleans, NaN,
infinity, negatives, fractional dispatches, or overflow; real-call limits must
be positive. Reservations and debits use integer minor currency units or
`Decimal`, never binary float. Exact-boundary admission is allowed and the
next block is refused.

The reservation alone is not a crash-safe debit. Immediately before every
actual `_run_child_agent` dispatch, an evaluation-only callback atomically
appends and fsyncs a unique debit record containing evaluation/block/condition,
stage, attempt, and dispatch ID. The call cannot begin until that durable debit
succeeds. Child completion appends a terminal debit record, but that alone
does not make a condition resumable. After the whole condition finishes, the
runner atomically appends and fsyncs its full bounded condition result plus
digest before writing a condition-committed marker.

On resume, a verified durable condition result is reused without dispatch even
if its committed marker or a debit-close record is missing; the journal may
reconcile those metadata records. If no verified condition result exists,
*any* debit for that condition—open or closed—is conservatively consumed and
the condition is sealed `crash_ambiguous`; it is never re-dispatched under the
same reservation. The runner emits a deterministic failed evaluation outcome
from durable journal facts, completes the other block arms within the original
ceiling, and keeps the uncertain qualified upper-bound cost charged. Re-running requires a
new evaluation/block and fresh approved budget; the old block is excluded from
paired analysis. Tests cover crashes after dispatch, after child return but
before result fsync, and after result fsync but before close/commit. This keeps
the exact 28/42 host-dispatch ceiling and conservatively consumes the
tariff-derived hard-cost reservation across every crash window.

Each evaluation receives a random 128-bit `evaluation_id`; each condition gets
a separate random 128-bit opaque ID. Every scorer-visible record carries the
evaluation ID plus the non-secret exact design descriptor
`factorial_2x2/block_size=4` or `lens_ablation/block_size=6`. The private
condition map stores IDs, settings, and schedule with owner-only permissions
and is reused on resume. Public hashes of known condition definitions are not
acceptable blinded IDs. Records with another evaluation ID, design kind, or
block size cannot be mixed or validated as a partial block.

Scoring is two-phase and append-only:

1. Phase A exposes only the redacted case rubric and safe sources, opaque
   condition ID, validated compact plan body/hash, and `plan_claims`
   deterministically enumerated from TL;DR bullets, numbered Next steps, and
   optional Risks bullets. It excludes candidate schema, candidate content,
   evidence arrays, execution state, model identity, and telemetry.
2. After Phase A raw judgments are complete, canonical consensus is derived,
   and the consensus plus raw-judgment manifest hashes are frozen, Phase B may
   expose the bounded process artifact:

- the already frozen `plan_claims` reappear only as mapping anchors, without
  Phase A scores, safe-source judgments, or consensus outcomes;
- `candidate_claims` are enumerated from the common
  `summary/steps/risks/verification` candidate fields and retain candidate
  index and local field pointer;
- V2 `evidence` records retain their candidate-local claim link, admitted
  locator status, support class, and mapped safe `case_source_id`; and
- bounded V2 assumptions/unknowns remain separate anchors so adjudicators can
  mark each candidate claim `grounded`, `counterevidence`, `explicitly_unresolved`,
  or `unsupported`; and
- `findings` use only the shared V1/V2 candidate fields for cross-condition
  comparison. V2 assumptions, unknowns, and evidence quality are scored
  separately.

Validated Phase B raw judgments are immutable; the validator derives a
canonical consensus JSONL and freezes separate raw-manifest and consensus
hashes exactly as in Phase A. Sizing and summary verify and re-derive all four
phase hashes; neither consumes an unhashed or mutable judgment file.

Synthesis may paraphrase, merge, split, or omit candidate claims. The host
therefore never assigns candidate evidence directly to a plan claim.
Adjudication explicitly maps each `candidate_claim_id` to zero, one, or
multiple `plan_claim_id` values and separately judges whether each safe source
directly supports, indirectly supports, contradicts, or is irrelevant/
insufficient for its candidate-local claim.

Primary factual plan support is arm-independent: blinded adjudicators map every
factual `plan_claim_id` directly to zero or more safe `case_source_id` values
and mark it `supported`, `unsupported`, or `explicitly_unresolved`. This direct
plan/source judgment controls unsupported-claim and critical-support metrics
for V1 and V2 alike. Candidate-to-plan and candidate-evidence mappings remain
separate V2 measures for citation-semantic fidelity and synthesis retention.
They have their own promotion floors but never grant a synthesized plan claim
automatic support credit. An unjudged mapping fails closed.

Production Candidate V1 parsing remains behavior-compatible, including
historically accepted permissive packets. A separate fail-closed evaluation
canonicalizer bounds and normalizes only the shared fields. A run whose V1 or
V2 candidate cannot be projected remains valid for production outcome
metrics, is excluded from claim/finding metrics, and increments a
condition-level unscoreable-artifact rate.

Before internal candidates are discarded, an optional private typed
evaluation sink receives only this bounded projection and admitted aggregate
metadata. The sink is an in-process dependency, not config or a production
result field. With no sink, production results, receipts, telemetry, and
visible output are unchanged. The sink never receives model identity, raw
locator, URL, evidence note, source excerpt, tool trace, error, or secret; it
may receive only evidence kind, locator status, and the host-resolved safe
`case_source_id`.

Each evaluation result also carries an exact identity-free execution
projection sourced directly from the structured `run_bestplan()` outcome and
host state, never parsed from a receipt:

```json
{
  "scheduled_attempts": 5,
  "terminal_attempts": 5,
  "valid_candidates": 4,
  "candidate_invalid": 1,
  "timed_out": 0,
  "quorum_required": 4,
  "quorum_met": true,
  "synthesis_started": true,
  "synthesis_succeeded": true,
  "interrupted": false
}
```

The key set and integer/boolean bounds are exact. It contains no attempt
identity, provider/model name, raw reason, exception, or receipt field. This
projection makes valid-candidate, quorum, and synthesis rates computable even
when invalid or timed-out candidates never reach the evaluation sink. Normal
host results always set `interrupted: false`; only the evaluation runner may
set it true from a durable open dispatch debit after a process crash.

The prohibition above applies to dedicated locator, source, telemetry, and
identity fields. Phase A deliberately includes the exact model-authored
compact plan body and derived claims, which may legitimately mention a
workspace-relative path, public URL, or model name. That bounded text is not
redacted because doing so would change the artifact/hash being judged. Case
preparation excludes credentials/private conversation data, and export runs a
secret-pattern sentinel check. The scorer receives no separate raw locator,
source URL/path, tool trace, evidence note, host model-identity field, or
blinded-condition map. Judgments are keyed by
`(evaluation_id, design_kind, case_id, blinded_condition_id, seed, phase,
score_group_id, judgment_id, scorer_id)`.

Executable rubric outcomes come only from a closed, versioned host-owned check
registry. Each check ID binds immutable check code, accepted inputs, expected
output schema, deterministic comparison rule, and fixed CPU/wall-clock/memory
limits. The host runs it with no network, no writable case tree, no mutable
checkout, and only the verified read-only snapshot. Side-effecting,
nondeterministic, unknown, timed-out, or non-zero-exit checks fail closed. The
runner persists a canonical invocation/result attestation containing input and
output digests, check-code/registry digest, limits, exit state, and stdout/
stderr digests without raw private output. Validation re-executes the check in
the same sandbox or verifies that attestation against an independently frozen
result; a submitted JSON `scorer.kind: executable` record alone can never
control consensus. The registry and runner digests are part of the experiment
design manifest.

Every subjective Phase A outcome is scored independently by two distinct
primary humans under one `score_group_id`; neither sees the other record. The
validator compares every controlling scalar, set, and plan/source judgment
after canonical sorting. Exact agreement produces a host-derived `unanimous`
consensus record referencing both primary judgment IDs and embedding the
complete canonical controlling outcome fields. Any field-level disagreement
requires a third human `adjudicator` record referencing those same two IDs and
resolving every disputed field; the host then derives a
`resolved_adjudication` consensus record that embeds the complete resolved
fields. Executable checks produce an `executable` consensus record linked to
the verified host attestation, closed check ID, registry digest, and embedded
canonical result. Raw primary, adjudicator, and executable-attestation records
are immutable.

The frozen Phase A artifact is the canonical consensus JSONL plus a manifest
hash binding the raw judgment file. Each consensus record also carries the raw
manifest hash, ordered controlling judgment IDs, and a digest of its embedded
canonical outcome. Validation recomputes all three from the immutable raw
judgments. The summary receives both files, verifies their manifest and
consensus hashes, re-derives every consensus record, and then consumes only
`unanimous`, `resolved_adjudication`, or `executable` outcomes. It reports raw
agreement and adjudication rates. Phase B binds to both frozen hashes and
cannot revise Phase A plan-success, constraint, critical-fact, or direct
plan/source judgments.

Every subjective Phase B outcome is likewise scored by two hidden independent
primary humans and the same exact-consensus/third-adjudicator rule. Its
canonical frozen manifest contains the semantic relation for every
candidate-evidence link: `direct_support`, `indirect_support`, `contradicts`,
or `irrelevant_or_insufficient`. Validation compares that relation with the
candidate-declared `direct|indirect|counterevidence` class; valid
counterevidence must contradict rather than entail the linked claim. Missing
or disagreed relations fail closed until adjudicated.

To keep judgment load bounded without weakening the grounding claim, Phase B
freezes two stages. Stage B1 duplicate-scores/adjudicates candidate criticality,
candidate-to-plan mappings, and findings. Stage B2 then duplicate-scores every
evidence link attached to a critical candidate claim and every declared
counterevidence link. A deterministic, pre-registered probability sample of
remaining noncritical direct/indirect links is descriptive only and never
substitutes for the controlling census. Before paid dispatch, preflight computes
the worst-case and expected Phase A/B record counts from the frozen case size,
attempt/evidence caps, and schedules, converts them with a frozen review-rate
assumption to scorer hours, and requires explicit record/hour ceilings. An
unfunded complete controlling census is `inconclusive` before model spend.

Together the two phases capture arm-independent plan/source support,
candidate evidence semantic fidelity, validity, gold-fact matching, uniqueness,
minority truth, and final-plan selection. Paraphrase, merge, and split
mappings are all representable.
This is sufficient to compute every primary metric without asking the
synthesizer to score itself. Missing, duplicate, mismatched, unbounded,
unmapped, or model-self-scored judgments are rejected before summary
generation.

### Pre-registered analysis sets and contrasts

Outcome metrics are intention-to-treat over every complete four-condition
`(case, seed)` block, including candidate-invalid, quorum-failed,
synthesis-failed, timed-out, and evaluation-unscoreable runs.

Claim, evidence, and finding contrasts use a paired complete-scoreability set:
a block enters only when every compared arm has a valid plan artifact and
every production-valid candidate in those arms has a bounded evaluation
projection and complete adjudication. A production-invalid candidate is a
real zero/failure, not an attrition exclusion. Report the included/excluded
block counts and per-condition unscoreable rates.

Promotion stops unless the one-sided 95% lower confidence bound for paired
scoreability is at least 90% and the one-sided 95% upper bound for the maximum
minus minimum per-condition unscoreable-rate spread is at most 5 percentage
points. The summarizer may not impute, use arm-specific denominators, or
promote from a selectively scoreable subset.

Pre-register the evidence-contract estimand in the protected condition map:

- primary: evidence-only (`evidence_v2/current`) minus baseline
  (`v1/current`);
- required replication/interaction check: combined
  (`evidence_v2/operational`) minus lenses-only (`v1/operational`); and
- lens estimand: combined minus evidence-only.

All evidence-promotion thresholds apply to the primary contrast, while the
replication contrast must agree in direction for verified success and
unsupported critical claims. A favorable primary with a reversed replication
result is a deterministic no-promote outcome, not permission to select the
better arm.

All final intervals use `family_cluster_percentile_v1`: exactly 10,000 draws
from `splitmix64_v1` with rejection-sampled indices and a fixed analysis seed
frozen in the pre-pilot design manifest, sizing report, and confirmatory map.
Corpus construction assigns a blinded `case_family_id` before any output,
keeps retry/variant/incident relatives wholly in pilot or holdout, and
de-duplicates when family membership cannot be defended. Each draw samples
exactly `n_h` family IDs with replacement inside each frozen primary category
stratum, retaining every case, seed, and condition in the sampled family, then
recomputes target-population-weighted estimands with the frozen category
weights. Point estimates, sizing influence vectors, and calibration use the
same weights/within-stratum sampling. A missing stratum or nonpositive frozen
weight is `inconclusive`. A one-sided alpha `.05`
gate uses the Type-1/nearest-rank empirical 5th or 95th percentile as
appropriate; the lens alpha-split gates below use the 2.5th or 97.5th
percentile. There is no BCa/basic/studentized interval, smoothing, retrying,
or dropped draw. If any controlling statistic is undefined or non-finite in
any draw, that gate is `inconclusive`. The sizing report and confirmatory map
bind the method/version, draw count, seed, family-manifest digest, and expected
quantile; summary verifies them and reports family as well as case counts.
Every summary also records the frozen size calculation, estimand names,
analysis-set coverage, and denominators.

Primary metrics:

- verified plan success;
- verified success on the pre-frozen `difficulty: easy` subset, with its
  paired denominator and coverage;
- critical constraint or root-cause miss rate;
- unsupported critical-claim rate;
- evidence locator resolution, semantic-relation precision, and critical
  candidate-claim coverage or explicit unresolved rate;
- critical-claim support recall;
- unique valid finding count and correlated miss rate;
- minority-truth retention and wrong-majority selection;
- valid-candidate/quorum/synthesis success rates; and
- p50/p95 latency, tokens, and qualified cost per verified successful plan.

The controlling latency metric is end-to-end host `total_latency_ms` from
condition admission through terminal classification, including failed,
timed-out, and interrupted intention-to-treat runs. It is not a sum of child
latencies and does not exclude unsuccessful plans. A process crash has no
comparable monotonic end timestamp, so a journal-sealed `crash_ambiguous` run
is charged the frozen per-condition `overall_timeout_ms` as
`total_latency_ms`, never null or a partial pre-crash duration.

Qualified cost per success uses one paired cost-complete analysis set: a block
enters only when every compared arm has complete qualified cost, and both the
cost numerator and verified-success denominator use only those same blocks.
Its coverage is reported against the ITT block set and must pass the 95%
coverage gate. An unknown-cost success is never retained only in the
denominator. If both baseline and treatment qualified cost per success are
exactly zero (for example, contractually included), the treatment/baseline
ratio is defined as `1`. If baseline is zero and treatment is positive, the
ratio is `+infinity` and fails the cost gate; baseline positive/treatment zero
defines ratio `0`. Included zero is never recoded as unknown.

Use the pre-registered `family_cluster_percentile_v1` confidence bounds under
the pre-registered contrasts and retain all cases/seeds for a sampled family. Every
controlling gate uses the conservative bound in the promotion direction, not
only a point estimate. Do not use a synthesizing model's self-score as ground
truth.

### Operational-lens ablation

Run this only if combined minus evidence-only passes the exact alpha-split
success-superiority or success-non-inferiority-plus-critical-miss rule below
plus the p95-latency and qualified-cost guardrails below, and the operator
separately approves its host-dispatch and cost cap.

The prerequisite comparison must be the count-five 2-by-2 result described
above, must be terminal confirmatory rather than pilot, and must carry the
same experiment-design digest, evidence contract, and exact full resolved
experiment fingerprint as the proposed ablation; a count-three or pool-A result cannot
justify promoting lenses that never ran or a pool-B ablation.
Use the operational contract with five attempts so every lens is reachable.
Create one immutable host-owned full-lens schedule and five matched ablation
schedules. Hold `candidate_contract: evidence_v2` fixed across all six
conditions. Each `(case, seed)` block uses the same pre-frozen Latin-square
row in all six arms. Each ablation keeps the same five explorer dispatches,
model order, tools, budgets, synthesizer, and lens-to-position assignment, but
replaces exactly one named lens checklist with its current neutral strategy
prompt wherever that lens occurs. Across complete five-block cycles every
lens is tested at every model position. This tests incremental lens
contribution without changing explorer-dispatch count or fixing one lens to
one model.

Treat `(case, seed)` as an atomic six-run block: full schedule plus the five
single-lens ablations. Blind with fresh random opaque IDs, reserve budget for
all six runs before dispatch, and resume or exclude partial blocks as a unit.
The schedules exist only in the evaluation harness. They do not add arbitrary
role strings, per-model roles, or an ablation surface to production
configuration.

Before dispatch, the protected ablation map records five fixed full-minus-
neutralized-lens estimands and the ordered success/non-inferior-critical-miss
rule in the promotion section below. Category strata cannot replace the
pre-registered overall metric. The current five-lens package is promoted only
if every lens passes; mixed results are deterministically `no_promote`.

The ablation pilot and sizing are separate from the 2-by-2 sizing record.
`freeze-lens-size` accepts only complete five-block Latin cycles and freezes
family-wise alpha `.05`, target joint all-five-package power `.80`, one RNG
seed, 10,000 pilot family-cluster bootstrap draws for the joint covariance, 100,000
non-nested multivariate-normal draws per candidate size using the frozen
`splitmix64_v1`/`box_muller_v1` contract, and the grid
`25, 30, ..., 200`. With a one-seed pilot it again assumes `rho=1`.
The joint vector contains each lens's success and critical-miss deltas;
missing/unstable covariance for either component is `inconclusive`. Package
latency/cost is already a controlling, pre-sized combined-versus-evidence-only
prerequisite; it is not re-tested five times in the contribution ablation.

For each lens, the success-superiority branch receives one-sided alpha `.025`
and therefore uses a 97.5% lower bound above zero. The alternative branch also
receives alpha `.025`; within that branch, success non-inferiority and
critical-miss superiority form an intersection-union test, so both use
one-sided 97.5% lower bounds, respectively above `-2` percentage points and
zero. The union of the two branches is therefore bounded by alpha `.05`.
All-five conjunction needs no further Bonferroni correction.

Power is evaluated against that exact alpha-split ordered OR rule in two package
alternatives: (S) all five lenses have `+5` percentage-point success delta and
zero critical-miss reduction; and (M) all five have zero success delta and
`+5` percentage-point critical-miss reduction. A draw passes only if every
lens passes through success superiority or through success non-inferiority
plus critical-miss superiority. The smallest multiple-of-five case count with
at least `.80` joint power under both S and M is frozen. Fewer than three
complete pilot cycles, missing denominators, unstable covariance, no passing
count by 200, or insufficient budget yields `inconclusive`.

The separate lens population/split must be the one frozen before the
factorial pilot. Related factorial or ablation cases cannot cross a
pilot/holdout boundary, and no post-factorial replacement or re-stratification
is allowed.
Lens confirmation uses the same handoff: a fresh
`init --design lens_ablation --phase confirmatory` consumes the frozen lens
sizing report, pilot ablation map, and pre-pilot lens holdout; selects only the
first frozen `N` eligible family-stratified IDs; excludes pilot cases/families;
binds the same full experiment fingerprint plus exactly three frozen seeds;
and creates a new confirmatory ID/map/source corpus. `run-lens-ablation` and
its summary require the lens-sizing, sampling-frame, selected-case, and design
digests; neither can reuse or mutate the pilot map.

### Promotion gates

Recommend `evidence_v2` only for the validated full experiment fingerprint
and frozen target task population/inclusion/strata if the
scoreability/attrition gates pass, the
pre-registered evidence-only-minus-baseline primary contrast passes every
applicable delta threshold below at matched budget, and the
combined-minus-lenses-only replication contrast agrees in direction.

Primary-contrast delta gates:

- verified plan success has a point improvement of at least 5 percentage
  points and a one-sided 95% lower bound above zero;
- unsupported critical claims have a point relative reduction of at least 25%
  and a one-sided 95% lower bound on reduction at least 25%;
- critical fact/constraint miss rate has a one-sided 95% upper bound on paired
  increase no greater than 2 percentage points in both the primary and
  replication contrasts, preventing promotion by simply omitting hard claims;
- easy-task success has a one-sided 95% lower bound on the paired delta above
  -2 percentage points, using only pre-frozen `difficulty: easy` cases; and
- p95 latency and qualified cost per verified success each have a one-sided
  95% upper bound on relative increase no greater than 25%.

Absolute evidence-quality gates apply separately to both evidence-bearing
arms, not to a V1 contrast: the one-sided 95% lower bound for valid admitted
evidence locators is at least 95%, and the corresponding lower bound for
the complete census of adjudicated evidence links attached to critical
candidate claims or declared `counterevidence` whose semantic relation matches
the declared class is at least 90%. Report direct/indirect/counterevidence
class denominators separately, but do not require a class whose opportunity
was not pre-frozen in the case rubric; missing natural counterevidence must
never incentivize fabrication. The lower bound for critical candidate claims with
class-consistent admitted evidence or explicit unresolved status is at least
90%. Separately, the lower bound for critical factual plan claims with
supported arm-independent direct plan/source judgments or explicit unresolved
status is at least 90%. Candidate evidence fidelity is controlling for the
grounding mechanism, while direct Phase A plan/source support remains
controlling for final-plan truth. A passing primary delta cannot override any
failed absolute floor.

The replication contrast must have favorable one-sided 95% bounds for verified
success and unsupported critical claims and must pass the critical-miss
non-inferiority gate. Qualified-cost comparison is
controlling only when the one-sided 95% lower bound for paired qualified-known-
cost coverage is at least 95% in every compared arm; otherwise promotion is
`inconclusive`, never zero-cost. Any absent, too-small, or incompletely
adjudicated controlling denominator is `inconclusive`.

Promote operational lenses only for the exact full frozen experiment
fingerprint if the combined-minus-evidence-only prerequisite passes this same
alpha-split ordered rule
and the combined arm's p95 latency and qualified cost-per-success ratios
versus evidence-only each have one-sided 95% upper bounds no greater than
`1.25`, with qualified-cost coverage at least 95%. Every one of the five
pre-registered ablations must then pass:

1. the full schedule improves verified plan success over that lens-neutral
   schedule with a one-sided 97.5% lower bound above zero; or
2. verified plan success is non-inferior with a one-sided 97.5% lower bound
   above -2 percentage points, and the reduction in critical-miss rate has a
   one-sided 97.5% lower bound above zero.

The fixed `.025/.025` branch allocation and intersection-union second branch
control every prerequisite, ablation, sizing simulation, and final decision.
The first satisfied branch controls; task-category results are descriptive
only. All five lenses must pass. Any mixed or failed result makes the current
operational package `no_promote`; this experiment does not remove a failed
lens after unblinding. A reduced package requires a new pre-registered design
and fresh confirmation. Otherwise retain current strategies and delete or
leave operational mode disabled.

A broader recommendation requires independent pre-registered confirmation
across every dimension it claims to generalize: materially different
host-mediated pools/orders and resolved versions, counts, synthesizers,
runtime modes, prompts/tool schemas, limits, and task populations/inclusion
rules/target strata. Merely passing two different pool orders or one narrow
task frame does not establish arbitrary-array, arbitrary-count,
model-agnostic, or population-wide behavior.

Neither experiment becomes the global default while the compiled BestPlan
pool contains `codex_app_server`. Global promotion requires a separate proven
native-tool containment/internal-accounting design or an explicitly approved
default-runtime change.

## Security and privacy

- Candidate packets, evidence, and tool traces are untrusted.
- File reads, searches, and locators are normalized, realpath-resolved, and
  restricted to the immutable host-issued `BestPlanReadScope`.
- Web/task evidence cannot cause a second unrestricted fetch or command.
- Evidence text and locators never appear in visible model ledgers or receipts.
- Evaluation privacy assertions distinguish structured hidden metadata from
  the exact bounded model-authored plan/candidate text that the experiment
  explicitly scores; sentinel secrets are rejected, but legitimate textual
  path/URL/model references are not silently rewritten.
- Existing reason-code sanitization remains the only failure detail shown.
- Evaluation cases are redacted before persistence.
- The API key previously pasted into chat remains exposed and must be rotated;
  it is not used, stored, or echoed by this work.

## Non-goals

- No static model-to-role assignments.
- No arbitrary role or prompt strings in configuration.
- No confidence-weighted voting.
- No evidence-count-weighted quorum.
- No new write-capable explorer tools.
- No provider fallback or K3 trust-policy change.
- No live configuration edit, credential write, model call, restart,
  deployment, or activation during source implementation.

## Research sources

- [Verification-Aware Planning for Multi-Agent Systems, EACL
  2026](https://aclanthology.org/2026.eacl-long.353/)
- [Mixture-of-Agents Enhances Large Language Model
  Capabilities](https://arxiv.org/abs/2406.04692)
- [ReConcile: Round-Table Conference Improves Reasoning via Consensus among
  Diverse LLMs](https://arxiv.org/abs/2309.13007)
- [Enabling Large Language Models to Generate Text with
  Citations](https://arxiv.org/abs/2305.14627)
- [ReAct: Synergizing Reasoning and Acting in Language
  Models](https://arxiv.org/abs/2210.03629)
- [When "A Helpful Assistant" Is Not Really Helpful, Findings of EMNLP
  2024](https://aclanthology.org/2024.findings-emnlp.888/)
- [Advancing Collaborative Debates with Role Differentiation, ACL
  2025](https://aclanthology.org/2025.acl-long.1105/)
- [Large Language Models Cannot Self-Correct Reasoning Yet, ICLR
  2024](https://proceedings.iclr.cc/paper_files/paper/2024/file/8b4add8b0aa8749d80a34ca5d941c355-Paper-Conference.pdf)
- [When Identity Skews Debate: Anonymization for Bias-Reduced Multi-Agent
  Reasoning, ACL 2026](https://aclanthology.org/2026.acl-long.650/)
