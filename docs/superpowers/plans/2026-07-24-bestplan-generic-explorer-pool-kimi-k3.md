# Generic BestPlan Explorer Pool and Kimi K3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Make BestPlan consume a validated, dynamic explorer array, use an explicitly named synthesizer, add Kimi K3 to Hermes' built-in Kimi Coding catalog, and emit truthful version-2 receipts without changing or restarting the live Hermes runtime.

**Architecture:** Keep provider resolution, scheduling, synthesis selection, and receipt integrity host-owned in `agent.bestplan_orchestrator`. Normalize the legacy two-lane schema at the validation boundary, then run only on the canonical `explorers` plus named `synthesizer` representation. The CLI reads that same normalized representation. Kimi K3 uses the existing `kimi-coding` provider, `anthropic_messages`, wire model `k3`, and the existing `KIMI_API_KEY` secret path.

**Tech Stack:** Python, pytest, YAML configuration, Hermes provider/runtime resolution, GitNexus

---

## Repositories and safety boundary

- Design and implementation-plan documents live in `/Users/seb/hermes-webui`.
- Agent source work happens only in the isolated worktree
  `/Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3`
  on branch `codex/bestplan-kimi-k3`.
- Use `scripts/run_tests.sh`; do not invoke bare pytest.
- Do not edit `~/.hermes/config.yaml`, `~/.hermes/.env`, the installed BestPlan
  skill, or any live state during source implementation.
- Do not restart, kill, deploy, or activate Hermes/WebUI.
- Never put an API key in a prompt, command argument, log, fixture, receipt, or
  committed file. Use a literal sentinel such as `SENTINEL_SECRET` in tests.
- Before editing a function, class, method, or catalog symbol, run GitNexus
  upstream impact analysis. Before each commit, run GitNexus
  `detect_changes(scope="staged", worktree=...)`.

The governing contract is
`/Users/seb/hermes-webui/docs/superpowers/specs/2026-07-24-bestplan-generic-explorer-pool-kimi-k3-design.md`.

### Task 1: Canonical dynamic explorer schema and legacy normalization

**Files:**

- Modify: `agent/bestplan_orchestrator.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`

**Working directory:** `/Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3`

**Step 1: Pass the pre-edit graph gate**

Codex runs GitNexus upstream impact for `validate_runtime` in
`agent/bestplan_orchestrator.py` and records the direct callers and risk before
Ornith edits. Stop and report before editing if the result is HIGH or CRITICAL.

**Step 2: Add failing canonical-schema tests**

Add focused tests proving:

- canonical config requires `explorers` and an explicitly named
  `synthesizer`;
- one through five explorers validate;
- six explorers, duplicate normalized names, unknown keys, empty strings,
  invalid API modes, invalid reasoning efforts, booleans-as-timeouts, and
  out-of-range timeouts fail closed;
- `ultra` is accepted only with `codex_app_server`, and
  `codex_app_server` is accepted only for an OpenAI provider;
- both `explorers` and legacy `lanes` in one block are rejected as ambiguous;
- a legacy `lanes` block preserves its order, treats its last entry as the
  synthesizer only in the adapter, and returns canonical `explorers` and
  `synthesizer`;
- canonical and legacy blocks both receive the documented `enabled` and
  timeout defaults when those optional values are omitted.

Use helper data with GLM, Kimi K3, and Sol. Assert the Kimi entry is exactly:

```python
{
    "name": "kimi-k3",
    "provider": "kimi-coding",
    "model": "k3",
    "api_mode": "anthropic_messages",
    "reasoning_effort": "max",
}
```

**Step 3: Run the RED tests**

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py -q
```

Expected: the new canonical-schema tests fail against the fixed
`{"glm", "sol"}` two-lane validator.

**Step 4: Implement strict normalization**

In `validate_runtime` and small private helpers:

- use compiled explorer/synthesizer defaults only when the entire BestPlan
  block is absent;
- apply the documented `enabled` and three timeout defaults to both explicit
  canonical and legacy blocks when those optional keys are omitted;
- accept canonical keys `enabled`, `explorers`, `synthesizer`,
  `explorer_timeout`, `synthesizer_timeout`, and `overall_timeout`;
- accept one to five canonical explorers;
- normalize names with `strip().lower()` and reject duplicates;
- require the synthesizer name to resolve to one configured explorer;
- validate exact explorer keys `name`, `provider`, `model`, `api_mode`, and
  `reasoning_effort`;
- validate the modes and efforts enumerated in the design;
- validate numeric timeout bounds without accepting booleans;
- normalize legacy `lanes` only when `explorers` is absent, preserving order
  and inferring only the legacy last entry as synthesizer;
- return only the canonical `explorers` and named `synthesizer` runtime shape.

Do not resolve credentials or make network calls during validation.

**Step 5: Run the GREEN tests**

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py -q
```

Expected: all schema and existing orchestrator tests pass.

**Step 6: Stage, inspect, run the graph gate, and commit**

```bash
git add agent/bestplan_orchestrator.py tests/agent/test_bestplan_orchestrator.py
git diff --cached
```

Codex runs GitNexus
`detect_changes(scope="staged", worktree="/Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3")`
and verifies only the intended BestPlan validation surface is affected.

```bash
git commit -m "feat(bestplan): normalize dynamic explorer pools"
```

### Task 2: Ordered explorer attempts and explicit synthesizer preflight

**Files:**

- Modify: `agent/bestplan_orchestrator.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`

**Working directory:** `/Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3`

**Step 1: Pass the pre-edit graph gate**

Codex records GitNexus upstream impact for `run_bestplan` and
`ExplorerResult`. The required user warning for `ExplorerResult`'s HIGH
transitive import fan-out has already been delivered. Avoid modifying that
public symbol: introduce a new private immutable attempt record and leave the
existing `ExplorerResult` constructor and exports unchanged.

**Step 2: Add failing orchestration tests**

Add tests proving:

- counts 2 through 5 cycle over the configured explorer list in invocation
  order;
- a one-entry pool repeats independent attempts;
- `/bestplan 3` over GLM, Kimi K3, and Sol constructs one child for each in
  that exact order;
- the named Sol explorer is used for synthesis even when it is not last;
- synthesizer credential/runtime preflight happens before any explorer child
  is constructed;
- synthesizer preflight failure produces zero child requests;
- unavailable Kimi is recorded as a failed attempt and does not silently
  substitute another model;
- quorum remains based on requested attempt count.

Introduce a small private immutable attempt record so each result carries
`index`, strategy, configured explorer identity, resolved identity when
available, status, and sanitized reason code. Do not modify `ExplorerResult`.

**Step 3: Run the RED tests**

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py -q
```

Expected: ordering and named-synthesizer tests fail because current code uses
`lanes`, picks the last available lane, and resolves explorers first.

**Step 4: Implement scheduling and preflight**

Update `run_bestplan` to:

- consume only normalized `explorers`;
- locate the named `synthesizer` before scheduling;
- resolve and construct the synthesizer locally before constructing explorer
  children, without making a model request;
- cycle attempts with `explorers[index % len(explorers)]`;
- preserve attempt order independently from concurrent completion order;
- never replace a failed explorer with another provider/model;
- always use the named synthesizer and never fall back to another lane.

Retain the existing bounded teardown and read-only child tool restrictions.

**Step 5: Run the GREEN tests**

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py -q
```

Expected: all focused tests pass.

**Step 6: Stage, inspect, run the graph gate, and commit**

```bash
git add agent/bestplan_orchestrator.py tests/agent/test_bestplan_orchestrator.py
git diff --cached
```

Codex runs staged GitNexus `detect_changes` against the exact Agent worktree
and reviews every affected direct importer before continuing.

```bash
git commit -m "feat(bestplan): schedule named explorer attempts"
```

### Task 3: Version-2 receipt schema and permanent v1 reading

**Files:**

- Modify: `agent/bestplan_orchestrator.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`
- Create: `tests/fixtures/bestplan_receipt_v1.json`

**Working directory:** `/Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3`

**Step 1: Pass the pre-edit graph gate**

Codex records GitNexus upstream impact for `make_receipt`,
`validate_receipt`, and `append_receipt`.

**Step 2: Check in a v1 compatibility fixture and failing v2 tests**

The v1 fixture contains only inert example values and a valid body hash. Add
tests proving:

- the v1 fixture remains readable and hash-valid;
- all new receipts use version 2 markers and schema;
- attempts are serialized in invocation-index order;
- each attempt contains only the exact fields defined by the design;
- configured and actual resolved provider/model identities remain distinct;
- synthesizer success records use the named synthesizer.

**Step 3: Run the RED tests**

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py -q
```

Expected: v2 schema and failed-receipt tests fail while the v1 fixture test
documents current compatibility behavior.

**Step 4: Implement the v2 writer and version-aware reader**

- Keep permanent constants/read support for v1 markers and version 1.
- Make all new writes use v2 markers and version 2.
- Build one logical receipt object for successful runs.
- Persist attempts with `index`, `strategy`, `explorer`, `configured`,
  nullable `resolved`, `status`, and nullable `reason_code`.
- Persist a synthesizer object with the same truthful configured/resolved
  split.
- Include the plan-body SHA-256 only when a body exists.

**Step 5: Run the GREEN tests**

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py -q
```

Expected: all receipt, sanitization, teardown, and orchestrator tests pass.

**Step 6: Stage, inspect, run the graph gate, and commit**

```bash
git add agent/bestplan_orchestrator.py tests/agent/test_bestplan_orchestrator.py tests/fixtures/bestplan_receipt_v1.json
git diff --cached
```

Codex runs staged GitNexus `detect_changes` against the exact Agent worktree.

```bash
git commit -m "feat(bestplan): write versioned receipts"
```

### Task 4: Persist every terminal BestPlan failure

**Files:**

- Modify: `agent/bestplan_orchestrator.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`

**Working directory:** `/Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3`

**Step 1: Pass the pre-edit graph gate**

Codex confirms the recorded upstream impacts for `run_bestplan`,
`make_receipt`, and `append_receipt` still cover the intended edits.

**Step 2: Add failing terminal-receipt tests**

Add tests for credential/preflight failure, child-construction failure,
quorum-unavailable, explorer timeout, overall timeout during construction,
exploration, and pre-synthesis, synthesizer timeout/network/empty output, and
incomplete teardown. For every terminal path assert:

- one failed v2 receipt is persisted;
- top-level `status` and allow-listed `reason_code` are present;
- attempts remain ordered and terminal;
- when synthesis never starts, `synthesizer.status == "not_started"`;
- when synthesis starts and fails, its configured/resolved identity and
  terminal status are truthful;
- no body hash, `body`, or plan-bearing `final_response` is returned.

**Step 3: Run the RED tests**

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py -q
```

Expected: failure-path persistence tests fail.

**Step 4: Implement one terminal receipt path**

Centralize finalization so every return from `run_bestplan` passes through one
version-2 persistence helper. Use only stable allow-listed reason codes. Never
fall back to an explorer body when synthesis fails.

**Step 5: Run the GREEN tests**

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py -q
```

Expected: all terminal-path and existing teardown tests pass.

**Step 6: Stage, inspect, run the graph gate, and commit**

```bash
git add agent/bestplan_orchestrator.py tests/agent/test_bestplan_orchestrator.py
git diff --cached
```

Codex runs staged GitNexus `detect_changes` against the exact Agent worktree.

```bash
git commit -m "feat(bestplan): persist terminal failure receipts"
```

### Task 5: Secret-safe reason mapping and output surfaces

**Files:**

- Modify: `agent/bestplan_orchestrator.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`
- Modify: `hermes_cli/subcommands/bestplan.py`
- Create or modify: `tests/hermes_cli/test_bestplan_cli.py`

**Working directory:** `/Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3`

**Step 1: Pass the pre-edit graph gate**

Codex confirms recorded upstream impacts for `run_bestplan`,
`cmd_bestplan`, and receipt helpers.

**Step 2: Add sentinel-secret tests**

Inject `SENTINEL_SECRET` into fake credential-resolution, child-construction,
child-runtime, timeout, persistence, and validation exceptions. Capture return
objects, receipts, CLI output, probe-style output, warnings, and logs. Assert
the sentinel and raw exception strings appear nowhere operator-visible or
durable, while stable reason codes do.

**Step 3: Run the RED tests**

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py tests/hermes_cli/test_bestplan_cli.py -q
```

Expected: at least one raw-exception output test fails before central
sanitization.

**Step 4: Implement central sanitization**

Map known exception classes and lifecycle states to a closed reason-code set.
Use static operator messages derived from those codes. Do not serialize,
interpolate, log, warn, or print raw exception text.

**Step 5: Run the GREEN tests**

```bash
scripts/run_tests.sh tests/agent/test_bestplan_orchestrator.py tests/hermes_cli/test_bestplan_cli.py -q
```

Expected: all sentinel and focused tests pass.

**Step 6: Stage, inspect, run the graph gate, and commit**

```bash
git add agent/bestplan_orchestrator.py tests/agent/test_bestplan_orchestrator.py hermes_cli/subcommands/bestplan.py tests/hermes_cli/test_bestplan_cli.py
git diff --cached
```

Codex runs staged GitNexus `detect_changes` against the exact Agent worktree.

```bash
git commit -m "fix(bestplan): sanitize terminal failure output"
```

### Task 6: CLI inspection and Kimi K3 catalog

**Files:**

- Modify: `hermes_cli/subcommands/bestplan.py`
- Modify: `hermes_cli/models.py`
- Modify: `agent/bestplan_orchestrator.py`
- Create: `tests/hermes_cli/test_bestplan_cli.py`
- Modify: `tests/hermes_cli/test_api_key_providers.py`
- Modify: `tests/hermes_cli/test_runtime_provider_resolution.py`
- Modify: `tests/agent/test_bestplan_orchestrator.py`

**Working directory:** `/Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3`

**Step 1: Pass the pre-edit graph gate**

Codex records or confirms GitNexus upstream impact for
`build_bestplan_parser`, `cmd_bestplan`, `_PROVIDER_MODELS`,
and `_resolve_lane_credentials`. Preserve the existing first `kimi-coding`
catalog entry because it is the silent default. GitNexus reports the shared
`resolve_runtime_provider` surface as CRITICAL, so this task must not edit it.

**Step 2: Add failing CLI, catalog, and runtime-resolution tests**

Add tests proving:

- `hermes bestplan lanes` prints canonical explorer rows in configured order;
- it prints `Synthesizer: sol` from normalized config;
- legacy config is displayed through the normalized canonical view;
- invalid config exits 1 with a sanitized validation message;
- `k3` is present in `_PROVIDER_MODELS["kimi-coding"]`;
- the existing first/default `kimi-coding` catalog item is unchanged;
- `k3` is not added to the separate legacy `moonshot` or
  `kimi-coding-cn` catalogs;
- an inert `sk-kimi-*` sentinel in the existing `KIMI_API_KEY` path resolves
  an explicitly requested `k3` through the Kimi Coding base
  `https://api.kimi.com/coding`, whose effective request path is
  `/v1/messages`, with `anthropic_messages`;
- BestPlan fails closed locally when legacy Moonshot credentials/endpoints
  resolve for K3 rather than dispatching a request.

**Step 3: Run the RED tests**

```bash
scripts/run_tests.sh tests/hermes_cli/test_bestplan_cli.py tests/hermes_cli/test_api_key_providers.py tests/hermes_cli/test_runtime_provider_resolution.py tests/agent/test_bestplan_orchestrator.py -q
```

Expected: canonical CLI tests fail and the K3 catalog assertion fails.

**Step 4: Update the CLI and catalog**

- Make CLI help describe `bestplan.explorers` and named synthesis.
- Call `validate_runtime` once and render its normalized `explorers` and
  `synthesizer`; do not independently merge config.
- Keep the command read-only.
- Add wire model `k3` to the built-in `kimi-coding` catalog only, after the
  existing first/default item.
- In BestPlan's private `_resolve_lane_credentials` boundary, when the
  configured provider/model are `kimi-coding`/`k3`, accept only the trusted
  `https://api.kimi.com/coding` resolved base and
  `anthropic_messages`; otherwise raise `BestPlanUnavailable` before child
  construction. Keep all non-K3 lanes and the shared runtime resolver
  unchanged.

**Step 5: Run the GREEN tests**

```bash
scripts/run_tests.sh tests/hermes_cli/test_bestplan_cli.py tests/hermes_cli/test_api_key_providers.py tests/hermes_cli/test_runtime_provider_resolution.py tests/agent/test_bestplan_orchestrator.py -q
```

Expected: all focused CLI/provider tests pass.

**Step 6: Stage, inspect, run the graph gate, and commit**

```bash
git add agent/bestplan_orchestrator.py hermes_cli/subcommands/bestplan.py hermes_cli/models.py tests/agent/test_bestplan_orchestrator.py tests/hermes_cli/test_bestplan_cli.py tests/hermes_cli/test_api_key_providers.py tests/hermes_cli/test_runtime_provider_resolution.py
git diff --cached
```

Codex runs staged GitNexus `detect_changes` against the exact Agent worktree.

```bash
git commit -m "feat(bestplan): expose Kimi K3 explorer configuration"
```

### Task 7: Source acceptance and integration review

**Files:**

- Review all files changed by Tasks 1-6.

**Step 1: Run the focused acceptance suite**

```bash
scripts/run_tests.sh \
  tests/agent/test_bestplan_orchestrator.py \
  tests/hermes_cli/test_bestplan_cli.py \
  tests/hermes_cli/test_api_key_providers.py \
  tests/hermes_cli/test_runtime_provider_resolution.py \
  -q
```

Expected: zero failures.

**Step 2: Run adjacent command/provider regressions**

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_commands.py \
  tests/hermes_cli/test_model_validation.py \
  tests/hermes_cli/test_overlay_slug_resolution.py \
  -q
```

Expected: zero failures and no change to ordinary chat, `/moa`, or delegation
selection.

**Step 3: Inspect scope**

Run GitNexus `detect_changes(scope="compare", base_ref="f529c7e3384e0317f076b7495465777882b0300a", worktree="/Users/seb/.config/superpowers/worktrees/hermes-agent/codex/bestplan-kimi-k3")`.

Expected: only BestPlan orchestration/CLI and the Kimi model catalog are
affected; investigate any unrelated process.

```bash
git status --short
git log --oneline --decorate -5
git diff --check f529c7e3384e0317f076b7495465777882b0300a..HEAD
```

**Step 4: Independent reviews**

Perform a spec-compliance review against the design document, then a separate
code-quality review. Any corrective edits must go back through a failing test,
Ornith implementation, focused verification, GitNexus scope review, and a
dedicated commit.

### Task 8: Prepare, but do not activate, local Hermes configuration

**Files (not changed during source work):**

- Later, with explicit activation approval: `~/.hermes/config.yaml`
- Later, entered by the human through the secure setup flow:
  `KIMI_API_KEY`
- Later, after source activation approval:
  `~/.hermes/skills/software-development/bestplan/SKILL.md`

**Step 1: Produce the intended non-secret block for operator review**

```yaml
bestplan:
  enabled: true
  explorers:
    - name: glm
      provider: custom:neuralwatt
      model: glm-5.2
      api_mode: chat_completions
      reasoning_effort: high
    - name: kimi-k3
      provider: kimi-coding
      model: k3
      api_mode: anthropic_messages
      reasoning_effort: max
    - name: sol
      provider: openai-codex
      model: gpt-5.6-sol
      api_mode: codex_app_server
      reasoning_effort: ultra
  synthesizer: sol
  explorer_timeout: 180
  synthesizer_timeout: 180
  overall_timeout: 540
```

Do not apply this block while the deployed agent still understands only
legacy `lanes`.

**Step 2: Require secure credential replacement**

The key pasted into chat is considered exposed. The human rotates it, then
enters the replacement through Hermes' normal Kimi provider setup so only
`KIMI_API_KEY` is stored. Codex and Ornith never receive or replay the secret.

**Step 3: Stop at the activation boundary**

Report source readiness, commits, test receipts, and the prepared config.
Request a separate activation window before updating the deployed source,
installed skill, or active config, and before any authenticated Kimi probe.
No process restart is implied by source completion.
