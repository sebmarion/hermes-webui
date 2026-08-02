# Context Budget Guard Implementation Plan

> **For Codex:** Use `superpowers:subagent-driven-development` to execute each task with a fresh Luna implementer, then a spec review and code-quality review. Keep both repositories in their dedicated `codex/context-budget-guard-*` worktrees.

**Goal:** Stop oversized provider requests before network I/O, compact the active session in place, and remove the blank-continuation recovery path.

**Architecture:** Hermes Agent owns one fail-closed admission receipt inside the final execution callback. Deterministic old-tool pruning and the existing compressor rewrite the same active `state.db` projection atomically, then the request is rebuilt and remeasured. WebUI keeps its visible transcript and reports irreducible failure on the current session.

**Tech stack:** Python 3.11-3.13, pytest through repository wrappers, vanilla JavaScript.

---

## Task 1: Final provider-request admission receipt

**Files:**

- Modify: `agent/chat_completion_helpers.py`
- Create: `tests/agent/test_provider_request_admission.py`

1. Add failing unit tests for Chat Completions and Responses payloads. Cover the 5%/1,024-token margin, explicit `max_tokens` / `max_completion_tokens` / `max_output_tokens`, the smaller threshold/window ceiling, non-positive limits, and compressor/Agent model-provider mismatch.
2. Run:

   ```bash
   scripts/run_tests.sh tests/agent/test_provider_request_admission.py
   ```

   Expected: fail because the receipt helper does not exist.
3. Add one pure `build_provider_request_admission_receipt(agent, api_kwargs)` helper using `estimate_request_context_tokens()`. Return only numeric/category sizes, resolved identity, decision, and reason; never include payload content.
4. Rerun the test and commit:

   ```bash
   git add agent/chat_completion_helpers.py tests/agent/test_provider_request_admission.py
   git commit -m "feat(context): add provider request admission receipt"
   ```

## Task 2: Deterministic tool pruning with atomic in-place persistence

**Files:**

- Modify: `agent/context_compressor.py`
- Modify: `agent/conversation_compression.py`
- Modify: `agent/agent_init.py`
- Create: `tests/agent/test_dispatch_tool_pruning.py`
- Modify: `tests/run_agent/test_in_place_compaction.py`

1. Add failing tests proving that every old tool result over 200 characters outside both `protect_last_n` and `tail_token_budget` becomes the existing informative receipt; large tool-call arguments are truncated; the newest identical output stays full; protected duplicates stay untouched; user/assistant text and `tool_call_id` values are unchanged; a second pass is a no-op.
2. Add failing persistence tests proving the rewrite calls `archive_and_compact()` once, old rows remain inactive/compacted, restart loads only the pruned active projection, a DB failure returns the original list, and the persistence cursor is re-baselined.
3. Run:

   ```bash
   scripts/run_tests.sh tests/agent/test_dispatch_tool_pruning.py tests/run_agent/test_in_place_compaction.py
   ```

   Expected: the new pruning/persistence tests fail.
4. Fix `_prune_old_tool_results()` so dedupe, summarization, and argument truncation all obey the computed prune boundary. Expose a small no-LLM `prune_tool_results_for_dispatch()` wrapper with the existing 200-character floor and both tail protections. Add `persist_in_place_projection()` beside the existing compression persistence code; mutate in-memory state only after the atomic DB rewrite succeeds.
5. Make automatic compression default to `in_place=True`; retain the explicit opt-out only for existing manual/legacy callers.
6. Rerun the tests and commit:

   ```bash
   git add agent/context_compressor.py agent/conversation_compression.py agent/agent_init.py tests/agent/test_dispatch_tool_pruning.py tests/run_agent/test_in_place_compaction.py
   git commit -m "feat(context): prune and persist dispatch history in place"
   ```

## Task 3: Wire the one final dispatch guard

**Files:**

- Modify: `agent/conversation_loop.py`
- Modify: `agent/chat_completion_helpers.py`
- Create: `tests/run_agent/test_provider_budget_guard.py`
- Modify: `tests/run_agent/test_413_compression.py`
- Modify: `tests/run_agent/test_run_agent.py`
- Modify: `tests/run_agent/test_run_agent_codex_responses.py`

1. Add failing integration tests proving request middleware, execution middleware, and Responses transport preflight all run before measurement; an oversized request never reaches either streaming or non-streaming transport; old tool history triggers one persisted prune/rebuild; successful compaction rebuilds and dispatches; irreducible input fails with `compression_exhausted=True` on the same session.
2. Add tests proving fallback rebinds the compressor and re-admits against the fallback ceiling without resetting the turn-wide three-attempt counter. A provider context rejection gets at most one retry and only after at least 5% estimated shrink. Completed tools are not executed again and their `tool_call_id` pairs survive.
3. Run:

   ```bash
   scripts/run_tests.sh tests/run_agent/test_provider_budget_guard.py tests/run_agent/test_413_compression.py tests/run_agent/test_run_agent.py tests/run_agent/test_run_agent_codex_responses.py
   ```

   Expected: the new guard assertions fail before implementation.
4. Immediately before building the provider copy, prune canonical `messages`. On a mutation, atomically persist, re-baseline with `conversation_history_after_compression()`, and restart request construction without spending a summary attempt.
5. Inside `_perform_api_call(next_api_kwargs)`, after Responses preflight and immediately before transport, build and record the content-free admission receipt, then dispatch or raise a typed local budget error. Do not classify that error as a provider/network failure. On budget rejection, use the existing compressor in place and share the existing maximum of three full-compression attempts across the logical turn.
6. Store the receipt on the Agent and emit one structured log containing only category sizes, limits, provider/model identity, and decision.
7. Remove only fallback-path `compression_attempts = 0` assignments. Keep the single turn-start initialization. Track one provider context-rejection retry across providers and require measurable shrink before it.
8. Rerun the focused tests and commit:

   ```bash
   git add agent/conversation_loop.py agent/chat_completion_helpers.py tests/run_agent/test_provider_budget_guard.py tests/run_agent/test_413_compression.py tests/run_agent/test_run_agent.py tests/run_agent/test_run_agent_codex_responses.py
   git commit -m "feat(context): enforce final provider budget guard"
   ```

## Task 4: Keep compression exhaustion on the current WebUI session

**Files:**

- Modify: `api/compression_recovery.py`
- Modify: `api/routes.py`
- Modify: `static/ui.js`
- Modify: `tests/test_compression_recovery_action.py`
- Modify: `tests/test_auto_compression_terminal_failure.py`

1. Replace the child-session tests with failing assertions that recovery metadata says `reduce_current_request`, the POST endpoint returns a current-session conflict without creating files/sessions, the card has no fork button, and a generic “continue” is intercepted with a narrow-request hint.
2. Run:

   ```bash
   ./scripts/test.sh tests/test_compression_recovery_action.py tests/test_auto_compression_terminal_failure.py
   ```

   Expected: fail against `start_focused_continuation` and child creation.
3. Change the recovery payload/copy to tell the user to reduce the request in the current session. Make `_handle_session_compression_recovery_start()` a non-mutating 409 compatibility response carrying the source session ID. Remove the UI fork/navigation action while retaining the current-session hint.
4. Rerun the tests and commit:

   ```bash
   git add api/compression_recovery.py api/routes.py static/ui.js tests/test_compression_recovery_action.py tests/test_auto_compression_terminal_failure.py
   git commit -m "fix(context): keep exhausted recovery in the active session"
   ```

## Task 5: Acceptance and neighboring regression suite

**Files:**

- Create: `tests/run_agent/test_context_budget_acceptance.py`
- Modify only if a failing invariant exposes a product defect in the files above.

1. Add a synthetic oversized fixture shaped like the observed 2.49 MB request. Assert four in-place compactions plus restart, fallback, and injected summary failure preserve exact objective, constraint, completed-work, and next-action markers; archived rows never rejoin active context; transport receives only admitted payloads; tool execution count stays one.
2. Run Agent acceptance and neighboring suites:

   ```bash
   scripts/run_tests.sh tests/agent/test_context_compressor.py tests/agent/test_dispatch_tool_pruning.py tests/agent/test_provider_request_admission.py tests/run_agent/test_context_budget_acceptance.py tests/run_agent/test_provider_budget_guard.py tests/run_agent/test_in_place_compaction.py tests/run_agent/test_compression_persistence.py tests/run_agent/test_infinite_compaction_loop.py tests/run_agent/test_compression_boundary.py tests/run_agent/test_413_compression.py tests/run_agent/test_run_agent_codex_responses.py
   ```
3. Run WebUI acceptance and neighboring suites:

   ```bash
   ./scripts/test.sh tests/test_compression_recovery_action.py tests/test_auto_compression_terminal_failure.py tests/test_issue1896_context_length_fallback_args.py tests/test_issue5270_cli_webui_continuity.py tests/test_issue5339_restart_stale_user_dedup.py
   ```
4. Inspect both diffs, confirm only the dedicated worktrees changed, and commit the acceptance test:

   ```bash
   git add tests/run_agent/test_context_budget_acceptance.py
   git commit -m "test(context): prove bounded continuity across compaction"
   ```

5. Do not merge, deploy, edit live state, or promote a release. Hand off the two reviewed branches and exact test evidence for Zeus-side release work later.
