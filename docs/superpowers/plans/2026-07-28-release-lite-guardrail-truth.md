# Release-Lite Guardrail Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let distinct failed terminal requests continue within the global iteration bound while preserving exact-repeat blocking, and render every genuine structured guardrail halt as `Needs recovery` instead of `Done`.

**Architecture:** The Agent change is a default-off config rule in the separate Agent worktree: only `terminal` bypasses the broad same-tool halt, while request-stage exact signatures, all other tools, policy, authorization, and max iterations remain authoritative. The WebUI change is unconditional correctness handling: classify structured Agent metadata before ordinary settlement, persist `guardrail_blocked` in the run journal, emit it to the browser, and keep the final explanation/tool output visibly in a non-success scene.

**Tech Stack:** Python dataclasses and YAML-backed config in Hermes Agent, Python streaming/run journal in Hermes WebUI, vanilla JavaScript rendering, pytest through each repository’s mandated wrapper.

---

## Preconditions and invariant ledger

- Agent worktree: `/Users/seb/hermes-webui/.worktrees/release-lite-agent`, branch `codex/release-lite-guardrail`.
- WebUI worktree: `/Users/seb/hermes-webui/.worktrees/release-lite-guardrail-webui`, branch `codex/release-lite-guardrail-webui`, created from the same approved base as the lazy-tail branch.
- Preserve user-owned `AGENTS.md` and `CLAUDE.md` modifications in both worktrees; never stage them.
- Agent tests use `scripts/run_tests.sh`; WebUI tests use `./scripts/test.sh`.
- Use `superpowers:test-driven-development`.
- Keep this guardrail WebUI work on its own branch/PR from lazy-tail. The two independently verified branches may be combined only by the sealed release integration step.
- Run GitNexus impact before editing every existing symbol and `detect_changes` before every commit.
- Existing impacts: Agent `ToolCallGuardrailConfig.from_mapping` LOW (one direct); Agent `after_batch` LOW (one direct); WebUI `_terminal_state_for_event` LOW (two direct). UNKNOWN/unindexed results must not be treated as safe.
- Agent behavior gate is config, not a new environment variable:

```yaml
tool_loop_guardrails:
  terminal_exact_failure_only: false
```

- WebUI `guardrail_blocked` mapping has no rollback gate and must remain enabled if the Agent behavior gate is disabled.
- No prose/text matching is allowed for guardrail truth.

## File map

Agent repository:

- Modify `agent/tool_guardrails.py`: parse the default-off rule, bypass only terminal broad-halt decisions, and emit explicit non-sensitive exact/broad counters in structured decision metadata.
- Modify `tests/agent/test_tool_guardrails.py`: pure exact/distinct/interleaved/non-terminal/default-off behavior.
- Modify `tests/run_agent/test_tool_call_guardrail_runtime.py`: live config and structured result regression.
- Modify `hermes_cli/config.py`: default config schema.
- Modify `cli-config.yaml.example`: documented example.
- Modify `website/docs/user-guide/configuration.md`: operator semantics and rollback.

WebUI repository:

- Modify `api/streaming.py`: structured guardrail classification, assistant terminal annotation, emitted done/error state.
- Modify `api/run_journal.py`: persist/replay `guardrail_blocked`.
- Modify `static/messages.js`: adopt structured done/error terminal state and reason.
- Modify `static/ui.js`: treat `guardrail_blocked` as errored/non-success and keep output open.
- Create `tests/test_guardrail_terminal_state.py`: structured classification, settlement, replay, browser rendering.
- Modify `tests/test_run_journal.py`: durable logical state.
- Modify `TESTING.md`: focused cross-repository verification and live scenario.

### Task 1: Add a default-off Agent rule without changing behavior

**Files:**
- Modify: `/Users/seb/hermes-webui/.worktrees/release-lite-agent/agent/tool_guardrails.py`
- Modify: `/Users/seb/hermes-webui/.worktrees/release-lite-agent/tests/agent/test_tool_guardrails.py`
- Modify: `/Users/seb/hermes-webui/.worktrees/release-lite-agent/hermes_cli/config.py`

- [ ] **Step 1: Run required impacts**

Run GitNexus impact in the Agent worktree for `ToolCallGuardrailConfig`, `from_mapping`, and the default config object in `hermes_cli/config.py`. Warn before editing on HIGH/CRITICAL.

- [ ] **Step 2: Write failing config tests**

Assert:

```python
assert ToolCallGuardrailConfig().terminal_exact_failure_only is False
assert ToolCallGuardrailConfig.from_mapping(
    {"terminal_exact_failure_only": True}
).terminal_exact_failure_only is True
assert ToolCallGuardrailConfig.from_mapping(
    {"terminal_exact_failure_only": "false"}
).terminal_exact_failure_only is False
```

Also assert the CLI default config contains the key with value `False`.

- [ ] **Step 3: Run and observe failure**

Run:

```bash
scripts/run_tests.sh tests/agent/test_tool_guardrails.py -q
```

Expected: FAIL because the field is absent.

- [ ] **Step 4: Implement the config field**

Add:

```python
terminal_exact_failure_only: bool = False
```

to `ToolCallGuardrailConfig`, parse it with `_as_bool`, and add the same false default under `tool_loop_guardrails` in `hermes_cli/config.py`. Do not add an environment variable and do not change existing thresholds.

- [ ] **Step 5: Run focused config tests**

Run the same command. Expected: PASS.

- [ ] **Step 6: Review and commit**

Run GitNexus `detect_changes(scope="all", worktree="/Users/seb/hermes-webui/.worktrees/release-lite-agent")`, then:

```bash
git add agent/tool_guardrails.py tests/agent/test_tool_guardrails.py hermes_cli/config.py
git commit -m "feat: gate terminal exact-failure mode"
```

### Task 2: Prevent broad terminal halts while preserving exact blocks

**Files:**
- Modify: `/Users/seb/hermes-webui/.worktrees/release-lite-agent/agent/tool_guardrails.py`
- Modify: `/Users/seb/hermes-webui/.worktrees/release-lite-agent/tests/agent/test_tool_guardrails.py`
- Modify: `/Users/seb/hermes-webui/.worktrees/release-lite-agent/tests/run_agent/test_tool_call_guardrail_runtime.py`

- [ ] **Step 1: Run impact for `before_call` and `after_batch`**

Record direct callers and affected execution flows. Also run impact for `ToolGuardrailDecision`, `ToolGuardrailDecision.to_metadata`, and `before_call`. `after_batch` is currently LOW; re-check against the refreshed index immediately before editing.

- [ ] **Step 2: Write four failing pure guardrail tests**

Create a helper with low thresholds:

```python
config = ToolCallGuardrailConfig(
    hard_stop_enabled=True,
    exact_failure_block_after=3,
    same_tool_failure_halt_after=2,
    terminal_exact_failure_only=True,
)
```

Assert:

1. four distinct `terminal` request-stage signatures never return `same_tool_failure_halt`;
2. the third identical terminal signature is blocked by `before_call` with `repeated_exact_failure_block`;
3. `A, B, A, C, A` retains A’s exact count and blocks A;
4. two distinct failing `web_search` signatures still produce `same_tool_failure_halt`.

Add a fifth test with the gate false proving two distinct terminal signatures retain the old broad halt.

Add focused safety assertions with the gate enabled:

6. `same_tool_failure_warning` is still emitted for distinct failing terminal calls at the warning threshold;
7. exact-signature warnings still precede the exact block;
8. an effect-capable/mutating tool retains its existing halt/recovery classification;
9. a synthetic `blocked by required policy` observation remains non-executed and cannot be bypassed or counted as a recoverable terminal failure;
10. authorization-screened calls remain rejected before execution.

For exact block and broad halt decisions, assert production metadata carries both counters without arguments:

```python
metadata = decision.to_metadata()
assert metadata["tool_name"] == "terminal"
assert metadata["exact_count"] == expected_exact_count
assert metadata["broad_count"] == expected_same_tool_count
assert "args" not in metadata
```

- [ ] **Step 3: Run and verify the correct failure**

Run:

```bash
scripts/run_tests.sh tests/agent/test_tool_guardrails.py -q
```

Expected: the gate-enabled distinct-terminal test FAILS with `same_tool_failure_halt`; existing tests remain green.

- [ ] **Step 4: Implement the narrow condition**

Keep exact counts unchanged. Extend `ToolGuardrailDecision` with optional `exact_count` and `broad_count` integer fields; `to_metadata()` includes them only when present and never adds request arguments. Populate both fields from the controller’s current counters for exact warnings/blocks and same-tool warnings/halts.

Replace only broad halt eligibility with a named predicate:

```python
def _same_tool_halt_applies(self, observation: ToolCallObservation) -> bool:
    return not (
        self.config.terminal_exact_failure_only
        and observation.tool_name == "terminal"
    )
```

and require that predicate in the existing `same_count >= same_tool_failure_halt_after` branch. Do not reset or skip `_exact_failure_counts`; do not modify request-stage signatures, warnings, other tools, recovery, authorization, or max iterations.

- [ ] **Step 5: Add runtime-config tests**

Through the real Agent config initialization, run distinct terminal failures and assert no controlled halt; repeat an identical command and assert:

```python
assert result["turn_exit_reason"] == "guardrail_halt"
assert result["guardrail"]["code"] == "repeated_exact_failure_block"
```

Also assert the default config retains existing broad terminal behavior.

Add one real run with `max_iterations=2` and distinct failing terminal signatures. Assert the result uses the existing max-iteration exit behavior, makes no more than two model/tool iterations, and never converts that outer bound into a guardrail recovery continuation. Reuse the existing required-policy, authorization, and mutation runtime fixtures and run them once with `terminal_exact_failure_only=True`; their decisions/results must be byte-for-byte or field-for-field unchanged from gate-off expectations.

Drive both a real exact-repeat halt and a real broad same-tool halt through `run_conversation`. Assert `result["guardrail"]` contains the structured tool name plus `exact_count` and `broad_count` copied from the live controller decision.

- [ ] **Step 6: Run focused Agent suites**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_tool_guardrails.py \
  tests/run_agent/test_tool_call_guardrail_runtime.py -q
```

Expected: all PASS.

- [ ] **Step 7: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add agent/tool_guardrails.py tests/agent/test_tool_guardrails.py tests/run_agent/test_tool_call_guardrail_runtime.py
git commit -m "fix: keep distinct terminal failures recoverable"
```

### Task 3: Document and seal the Agent rollout gate

**Files:**
- Modify: `/Users/seb/hermes-webui/.worktrees/release-lite-agent/cli-config.yaml.example`
- Modify: `/Users/seb/hermes-webui/.worktrees/release-lite-agent/website/docs/user-guide/configuration.md`

- [ ] **Step 1: Add config documentation**

Document:

```yaml
tool_loop_guardrails:
  terminal_exact_failure_only: false
```

State precisely: when true, distinct `terminal` signatures do not trigger the broad same-tool halt; identical signatures still block; warnings and global iterations remain; other tools are unchanged. Rollback sets the key false. Do not suggest mutating a real user config during tests.

- [ ] **Step 2: Verify documentation and default schema**

Run:

```bash
rg -n "terminal_exact_failure_only" \
  agent/tool_guardrails.py hermes_cli/config.py cli-config.yaml.example \
  website/docs/user-guide/configuration.md
scripts/run_tests.sh \
  tests/agent/test_tool_guardrails.py \
  tests/run_agent/test_tool_call_guardrail_runtime.py -q
```

Expected: four implementation/documentation locations and all tests PASS.

- [ ] **Step 3: Run full Agent tests**

Run:

```bash
scripts/run_tests.sh
```

Expected: exit 0. Record exact passed/skipped counts.

- [ ] **Step 4: Review and commit**

Run GitNexus `detect_changes(scope="compare", base_ref="main", worktree=...)`, then:

```bash
git add cli-config.yaml.example website/docs/user-guide/configuration.md
git commit -m "docs: explain terminal exact-failure rollout"
```

### Task 4: Classify structured guardrail results before WebUI settlement

**Files:**
- Modify: `api/streaming.py`
- Create: `tests/test_guardrail_terminal_state.py`

- [ ] **Step 1: Run WebUI impacts**

In `/Users/seb/hermes-webui/.worktrees/release-lite-guardrail-webui`, run impact for `_agent_result_tool_limit_reached`, the result-processing function around its call at `api/streaming.py:9219`, `_mark_latest_assistant_tool_limit_status`, and the done-payload function. UNKNOWN means tests must cover the call seam.

- [ ] **Step 2: Write failing structured-classifier tests**

Assert:

```python
assert _agent_result_guardrail_blocked({
    "turn_exit_reason": "guardrail_halt",
    "guardrail": {"action": "halt", "code": "same_tool_failure_halt"},
}) == GuardrailTerminal("same_tool_failure_halt")

assert _agent_result_guardrail_blocked({
    "guardrail": {"action": "block", "code": "required_policy_block"},
}) == GuardrailTerminal("required_policy_block")

assert _agent_result_guardrail_blocked({
    "final_response": "I stopped because guardrail halt ..."
}) is None
```

Malformed structured metadata with `turn_exit_reason=guardrail_halt` must return generic reason `guardrail_halt`, never success.

- [ ] **Step 3: Write failing settlement tests**

Drive the streaming result seam and assert:

```python
assert assistant["_terminal_state"] == "guardrail_blocked"
assert assistant["_terminal_reason"] == "same_tool_failure_halt"
assert done_payload["terminal_state"] == "guardrail_blocked"
assert done_payload["terminal_reason"] == "same_tool_failure_halt"
assert assistant_explanation_is_visible
assert last_tool_output_is_visible
```

Assert a guardrail block takes precedence over ordinary `done` classification and does not emit a successful completion card. Keep existing tool-limit tests unchanged.

- [ ] **Step 4: Run and verify failure**

Run:

```bash
./scripts/test.sh tests/test_guardrail_terminal_state.py -q
```

Expected: FAIL because `_agent_result_guardrail_blocked` is absent.

- [ ] **Step 5: Implement structured-only classification**

Add a small immutable result type and helper that checks only:

- exact structured `turn_exit_reason == "guardrail_halt"`; or
- `result["guardrail"]["action"] in {"block", "halt"}`.

Normalize the reason to a bounded stable code; malformed/missing code becomes `guardrail_halt`. Add `_mark_latest_assistant_guardrail_status(messages, reason)` mirroring the existing tool-limit annotation but setting:

```python
msg["_terminal_state"] = "guardrail_blocked"
msg["_terminal_reason"] = reason
```

Compute this before ordinary settlement, preserve final/tool messages, and add state/reason to emitted terminal payloads. Do not infer from strings in `final_response`, errors, or transcript text.

- [ ] **Step 6: Run streaming regression tests**

Run:

```bash
./scripts/test.sh \
  tests/test_guardrail_terminal_state.py \
  tests/test_tool_limit_terminal_state.py -q
```

Expected: all PASS.

- [ ] **Step 7: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add api/streaming.py tests/test_guardrail_terminal_state.py
git commit -m "fix: classify guardrail stops as blocked"
```

### Task 5: Persist and replay `guardrail_blocked`

**Files:**
- Modify: `api/run_journal.py`
- Modify: `tests/test_run_journal.py`
- Modify: `tests/test_guardrail_terminal_state.py`

- [ ] **Step 1: Re-run impact for `_terminal_state_for_event`**

Current result is LOW with two direct dependents. Inspect both before changing the accepted state set.

- [ ] **Step 2: Write failing journal tests**

Append:

```python
append_run_event(
    "session_1", "run_guardrail", "done",
    {"terminal_state": "guardrail_blocked",
     "terminal_reason": "same_tool_failure_halt"},
    session_dir=tmp_path,
)
```

Assert `latest_run_summary` and `find_run_summary` preserve `guardrail_blocked`, and a later `stream_end` cannot downgrade it to completed. Assert malformed explicit states still fall back safely under existing rules.

- [ ] **Step 3: Run and verify failure**

Run:

```bash
./scripts/test.sh tests/test_run_journal.py tests/test_guardrail_terminal_state.py -q
```

Expected: FAIL because done events only recognize `tool_limit_reached`.

- [ ] **Step 4: Extend the durable logical state**

Allow `guardrail_blocked` in the explicit terminal state set for `done`/`stream_end`. Preserve sticky logical terminal-state precedence so later transport settlement cannot overwrite it.

- [ ] **Step 5: Run journal regressions**

Run the Task 5 command. Expected: all PASS.

- [ ] **Step 6: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add api/run_journal.py tests/test_run_journal.py tests/test_guardrail_terminal_state.py
git commit -m "fix: persist guardrail blocked task state"
```

### Task 6: Render `Needs recovery` and keep the blocked output visible

**Files:**
- Modify: `static/messages.js`
- Modify: `static/ui.js`
- Modify: `tests/test_guardrail_terminal_state.py`

- [ ] **Step 1: Run impacts**

Run GitNexus impact for the messages done-event handler, `_anchorSceneHasErroredTerminalState`, and the status-card renderer. Record UNKNOWN results and cover those exact source seams in tests.

- [ ] **Step 2: Write failing browser-source tests**

Assert `guardrail_blocked` belongs to all error/non-success terminal-state sets, done payload adoption copies `terminal_state` and `terminal_reason` to the settled anchor, and the rendered title is exactly `Needs recovery`. Assert:

- no `Done` label for a blocked scene;
- assistant explanation and last tool output remain expanded/visible;
- replay/hydration produces the same state;
- a later plain done event cannot downgrade it.

- [ ] **Step 3: Run and verify failure**

Run:

```bash
./scripts/test.sh tests/test_guardrail_terminal_state.py -q
```

Expected: FAIL because the browser does not recognize `guardrail_blocked`.

- [ ] **Step 4: Implement browser adoption**

In `static/messages.js`, pass structured terminal state/reason from done/error events into the anchor/scene using existing stable scene ownership guards. In `static/ui.js`, add `guardrail_blocked` to both non-success/error terminal sets and map it to the status-card title `Needs recovery`. Do not match explanation prose.

- [ ] **Step 5: Run UI and streaming tests**

Run:

```bash
./scripts/test.sh \
  tests/test_guardrail_terminal_state.py \
  tests/test_tool_limit_terminal_state.py \
  tests/test_issue5941_errored_turn_response_visible.py \
  tests/test_run_journal.py -q
```

Expected: all PASS.

- [ ] **Step 6: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add static/messages.js static/ui.js tests/test_guardrail_terminal_state.py
git commit -m "fix: show guardrail blocks as needs recovery"
```

### Task 7: Add non-sensitive guardrail mapping diagnostics

**Files:**
- Modify: `api/streaming.py`
- Modify: `api/routes.py`
- Modify: `tests/test_guardrail_terminal_state.py`
- Create: `tests/test_guardrail_observability.py`

- [ ] **Step 1: Write failing diagnostic tests**

Drive a real Agent-produced structured halt fixture from the runtime test helper (not a hand-authored result dictionary) through WebUI classification and assert one diagnostic record contains:

```python
{
    "event": "guardrail_terminal_mapped",
    "guardrail_code": "same_tool_failure_halt",
    "tool_name": "terminal",
    "exact_count": 1,
    "broad_count": 4,
    "terminal_state": "guardrail_blocked",
}
```

The real fixture must first assert its `result["guardrail"]` contains `tool_name`, `exact_count`, and `broad_count`. When legacy Agent metadata lacks a count or a malformed future payload supplies one, WebUI records `null`/omits it rather than deriving it from prose. Assert diagnostics never contain command, arguments, transcript text, paths, tool output, `final_response`, or arbitrary nested metadata.

- [ ] **Step 2: Run and verify failure**

Run:

```bash
./scripts/test.sh \
  tests/test_guardrail_observability.py \
  tests/test_guardrail_terminal_state.py -q
```

Expected: FAIL because no bounded mapping diagnostic exists.

- [ ] **Step 3: Implement bounded structured diagnostics**

At the structured classification seam, copy only validated scalar fields from `result.guardrail`: stable code, bounded tool name, exact/broad integer counts when supplied, and the fixed mapped terminal state. Emit through the existing server diagnostic/logger path. Do not log the complete `result`, signature arguments/hash, message text, paths, or tool result.

If Agent metadata currently provides only one `count`, store it under the count kind named by its structured code and leave the other absent; do not guess.

- [ ] **Step 4: Run diagnostic and settlement regressions**

Run the Task 7 command plus:

```bash
./scripts/test.sh tests/test_client_event_logging.py -q
```

If the sanitizer test has a different name, locate it with `rg --files tests | rg 'client.*event'`. Expected: all PASS.

- [ ] **Step 5: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add api/streaming.py api/routes.py tests/test_guardrail_terminal_state.py tests/test_guardrail_observability.py
git commit -m "feat: record bounded guardrail mapping diagnostics"
```

### Task 8: Cross-repository verification and release evidence

**Files:**
- Modify: `TESTING.md`

- [ ] **Step 1: Document focused verification**

Add the Agent and WebUI commands, the default-off Agent gate, unconditional WebUI mapping, and isolated-state live scenarios. Do not edit `CHANGELOG.md`.

- [ ] **Step 2: Run full Agent verification**

From `/Users/seb/hermes-webui/.worktrees/release-lite-agent`:

```bash
scripts/run_tests.sh
```

Expected: exit 0. Record exact counts.

- [ ] **Step 3: Run focused WebUI verification**

From `/Users/seb/hermes-webui/.worktrees/release-lite-guardrail-webui`:

```bash
./scripts/test.sh \
  tests/test_guardrail_terminal_state.py \
  tests/test_guardrail_observability.py \
  tests/test_tool_limit_terminal_state.py \
  tests/test_run_journal.py \
  tests/test_issue5941_errored_turn_response_visible.py -q
```

Expected: all PASS.

- [ ] **Step 4: Run full WebUI verification**

Run:

```bash
./scripts/test.sh
```

Expected: exit 0. Record exact counts.

- [ ] **Step 5: Run final change detection in both worktrees**

Agent:

```text
detect_changes(scope="compare", base_ref="main",
  worktree="/Users/seb/hermes-webui/.worktrees/release-lite-agent")
```

WebUI:

```text
detect_changes(scope="compare", base_ref="master",
  worktree="/Users/seb/hermes-webui/.worktrees/release-lite-guardrail-webui")
```

Expected affected scope: Agent guardrail config/count decision paths; WebUI streaming settlement, run-journal terminal classification, and scene rendering. Investigate any unrelated flow before continuing.

- [ ] **Step 6: Commit testing guidance**

```bash
git add TESTING.md
git commit -m "test: document guardrail recovery verification"
```

- [ ] **Step 7: Perform isolated live acceptance**

Use isolated Hermes/WebUI state. With the Agent gate enabled:

1. force four distinct failing terminal request signatures and verify the turn continues until another outcome or max iterations;
2. force the same failed terminal signature three times at the live exact threshold and verify the Agent returns structured `guardrail_halt`;
3. verify WebUI settles the latter as `guardrail_blocked` / `Needs recovery`;
4. verify both the task header and settled anchor say `Needs recovery`, while the final explanation and tool output remain visible;
5. reload/replay and verify it never becomes `Done`;
6. disable the Agent gate and verify legacy broad-halt behavior returns while WebUI still maps it honestly.

Never edit the real `~/.hermes/config.yaml` as part of automated verification.

- [ ] **Step 8: Request review and verify before completion**

Use `superpowers:requesting-code-review` on both diffs, resolve blocking findings, rerun focused suites, and use `superpowers:verification-before-completion` before any completion claim.
