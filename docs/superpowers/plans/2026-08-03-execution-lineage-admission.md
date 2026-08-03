# Execution-Lineage Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent compression/tool-limit aliases from admitting concurrent WebUI turns, while preserving the exact deferred work and keeping the original physical session target/profile intact.

**Architecture:** Resolve one validated execution-lineage key from the profile state database path and the compression/tool root. Bind that key to the existing run-admission reservation under `ACTIVE_RUNS_LOCK`, transfer it atomically into `ACTIVE_RUNS`, and use the same key for the existing process-local deferred-wakeup buckets. Parent teardown retries the already-durable tool child after unregistering and before the generic deferred drain. No new scheduler, lock family, durable queue, or schema is introduced.

**Tech Stack:** Python 3.11–3.13, existing Hermes WebUI registries/locks, JSON tool receipts, SQLite shared-session resolver, pytest through `./scripts/test.sh`.

---

### Task 1: Add the shared execution-lineage resolver

**Files:**
- Create: `api/execution_lineage.py`
- Test: `tests/test_execution_lineage_admission.py`

- [ ] **Step 1: Write failing resolver tests**

  Cover a missing first-turn `state.db` row, compression root/tip equivalence, invalid/degraded/ambiguous resolution, equal IDs in different profiles, and a tool child whose session metadata and durable receipt agree. Add a conflicting/missing receipt case that must fail closed.

- [ ] **Step 2: Run the resolver tests and verify the expected failures**

  Run: `./scripts/test.sh tests/test_execution_lineage_admission.py -q`

  Expected: failures because the resolver module/API does not yet exist.

- [ ] **Step 3: Implement the minimal resolver**

  Add a small immutable result type and typed unavailable error. Validate the profile name before calling `get_hermes_home_for_profile`; derive the absolute `<profile-home>/state.db` path without falling back to the active profile. For tool-limit children, require matching `Session.root_session_id`, control metadata, and an exact child receipt (`child_session_id`, `root_session_id`, `execution_id`, and normalized profile). Resolve only bounded compression ancestry with `resolve_shared_session(..., mode="history")`; accept `missing` as a first-turn fallback and reject `degraded`/`ambiguous`. Hash a canonical versioned `(state_db_path, execution_root)` payload with SHA-256. Keep paths, roots, and receipt data out of the returned public metadata except for short-lived routing fields.

- [ ] **Step 4: Run the resolver tests and verify they pass**

  Run: `./scripts/test.sh tests/test_execution_lineage_admission.py -q`

  Expected: resolver cases pass, including fail-closed profile/receipt behavior.

- [ ] **Step 5: Commit the resolver slice**

  ```bash
  git add tests/test_execution_lineage_admission.py api/execution_lineage.py
  git commit -m "fix: resolve WebUI execution lineages"
  ```

### Task 2: Bind lineage ownership to existing admission state

**Files:**
- Modify: `api/config.py:10199-10301,11042-11142`
- Test: `tests/test_execution_lineage_admission.py`

- [ ] **Step 1: Write failing admission tests**

  Test that a bound reservation blocks an ancestor/tip or tool-child reservation before `ACTIVE_RUNS` registration; that a bound reservation transfers its immutable key atomically into `ACTIVE_RUNS`; that duplicate active keys are rejected; that `update_active_run()` cannot replace/remove the key; and that auxiliary unkeyed runs remain allowed.

- [ ] **Step 2: Run the admission tests and verify the expected failures**

  Run: `./scripts/test.sh tests/test_execution_lineage_admission.py -q`

  Expected: the new ownership assertions fail against exact-session-only admission.

- [ ] **Step 3: Implement atomic binding and transfer**

  Add a typed lineage-busy error and a `bind_run_admission(reservation_id, key)` helper. Compute/validate lineage outside the lock; under `ACTIVE_RUNS_LOCK`, compare the key against all other bound reservations and active entries, then attach it idempotently. Make `register_active_run()` inherit the bound key in the existing reservation-to-active critical section, reject missing keys for explicitly turn-bearing workers, and re-check duplicate keys under the same lock. Make `update_active_run()` reject key mutation. Keep checkpoint/health/admission projections whitelisted or explicitly redacted.

- [ ] **Step 4: Run the admission tests and verify they pass**

  Run: `./scripts/test.sh tests/test_execution_lineage_admission.py -q`

  Expected: reservation, transfer, immutability, auxiliary-overlap, and redaction tests pass.

- [ ] **Step 5: Commit the admission slice**

  ```bash
  git add tests/test_execution_lineage_admission.py api/config.py
  git commit -m "fix: serialize WebUI runs by execution lineage"
  ```

### Task 3: Bind every local turn-bearing entrypoint before mutation

**Files:**
- Modify: `api/routes.py:23111-23240,23243-23368,23634-23820,24391-24630,27433-27540`
- Modify: `api/streaming.py:7720-7790`
- Modify: `api/gateway_chat.py:800-875`
- Test: `tests/test_execution_lineage_admission.py`

- [ ] **Step 1: Write failing entrypoint tests**

  Exercise native/local chat, server wakeup, tool/goal continuation, manual compression, `/btw`, and ordinary background creation. Assert the reservation is bound before the first sidecar/journal/child save mutation, that busy returns 409 without those mutations, and that `/btw`/background hidden children use independent keys. Add a missing-key rejection test for native/Gateway turn-bearing worker registration while auxiliary workers remain unkeyed.

- [ ] **Step 2: Run the entrypoint tests and verify the expected failures**

  Run: `./scripts/test.sh tests/test_execution_lineage_admission.py -q`

  Expected: the current entrypoints admit aliases or mutate before binding.

- [ ] **Step 3: Implement earliest binding and worker classification**

  Add a route helper that resolves/binds the current reservation and maps busy to the existing 409 response and unavailable identity to retryable 503. Call it immediately after loading the physical session/profile in `start_session_turn()` and before pause/delegation mutations; call it defensively/idempotently at `_start_chat_stream_for_session()`. Bind `/btw` and background reservations to their newly allocated hidden child before its first save. Bind manual compression before job-state mutation. Mark native/Gateway conversation workers as lineage-required and keep cron/finalizer/recovery/sessionless helpers auxiliary. Preserve external-runner ownership out of this local bind.

- [ ] **Step 4: Run the entrypoint tests and verify they pass**

  Run: `./scripts/test.sh tests/test_execution_lineage_admission.py -q`

  Expected: all entrypoint ordering, classification, native, and Gateway cases pass.

- [ ] **Step 5: Commit the entrypoint slice**

  ```bash
  git add tests/test_execution_lineage_admission.py api/routes.py api/streaming.py api/gateway_chat.py
  git commit -m "fix: bind all WebUI turn entrypoints"
  ```

### Task 4: Make deferred wakeups lineage-owned and profile-safe

**Files:**
- Modify: `api/background_process.py:1120-1215,1293-1338,1499-1778,1790-1915`
- Modify: `api/routes.py` (wakeup dispatch signature/expected-profile validation)
- Test: `tests/test_wakeup_defer_race.py`
- Test: `tests/test_execution_lineage_admission.py`

- [ ] **Step 1: Write the incident regression tests**

  Reproduce root `R` active → ancestor wakeup deferred → tool child `C` claimed/attempted → parent unregister. Assert the child is the only admitted owner, the ancestor wakeup stays queued, child teardown drains the ancestor bucket once, and the retained target profile is used. Add a three-segment receipt test and a cross-profile same-ID test.

- [ ] **Step 2: Run the wakeup tests and verify the expected failures**

  Run: `./scripts/test.sh tests/test_wakeup_defer_race.py tests/test_execution_lineage_admission.py -q`

  Expected: current exact-session buckets allow both starts or strand an intermediate segment.

- [ ] **Step 3: Implement lineage-keyed queue ownership**

  Change only the existing `DEFERRED_PROCESS_WAKEUPS` bucket key to the opaque lineage key. Store `target_session_id` and normalized `target_profile` on each entry; preserve process ID, prompt, delegation ID, and completion event. Reuse the live key when recording against an occupied lineage. Make active checks count bound reservations and active keys. Drain one entry at a time, requeue entries two through N before launching entry one, pass `expected_profile` through dispatch, and requeue on 409/fence/profile mismatch. Keep durable async-delegation schema/replay unchanged while routing every start through admission; do not claim generic process-local wakeups survive restart.

- [ ] **Step 4: Run the wakeup tests and verify they pass**

  Run: `./scripts/test.sh tests/test_wakeup_defer_race.py tests/test_execution_lineage_admission.py -q`

  Expected: the reproduced race has one winner, deferred prompts remain ordered, and existing 409/requeue tests stay green.

- [ ] **Step 5: Commit the deferred-wakeup slice**

  ```bash
  git add tests/test_wakeup_defer_race.py tests/test_execution_lineage_admission.py api/background_process.py api/routes.py
  git commit -m "fix: defer wakeups by execution lineage"
  ```

### Task 5: Order native/Gateway teardown and continuation recovery

**Files:**
- Modify: `api/streaming.py:11903-12025`
- Modify: `api/gateway_chat.py:1460-1545`
- Modify: `api/tool_limit_continuation.py:600-790`
- Test: `tests/test_tool_limit_continuation.py`
- Test: `tests/test_wakeup_defer_race.py`

- [ ] **Step 1: Write failing teardown-order tests**

  Assert a tool child is not launched while its parent still owns the lineage; after unregister, recovery filtered to the current execution root runs before goal recovery and generic deferred draining. Assert native and Gateway paths share the same ordering and a failed/busy child remains durably claimed.

- [ ] **Step 2: Run the teardown tests and verify the expected failures**

  Run: `./scripts/test.sh tests/test_tool_limit_continuation.py tests/test_wakeup_defer_race.py -q`

  Expected: current `handle_terminal()` launches before unregister and Gateway teardown omits the generic lineage drain.

- [ ] **Step 3: Implement post-unregister recovery ordering**

  Make tool-limit terminal handling create/claim the durable child but defer its start until after parent `unregister_active_run()`. Add a root-filtered recovery helper or wrapper around existing receipts. Invoke tool recovery first, existing goal recovery second, and the lineage-aware generic drain third from one shared teardown helper used by native and Gateway. Preserve durable claim/retry behavior on launch failure and startup replay.

- [ ] **Step 4: Run the teardown tests and verify they pass**

  Run: `./scripts/test.sh tests/test_tool_limit_continuation.py tests/test_wakeup_defer_race.py -q`

  Expected: child priority, exactly-once ancestor delivery, receipt durability, and both backend orderings pass.

- [ ] **Step 5: Commit the teardown slice**

  ```bash
  git add tests/test_tool_limit_continuation.py tests/test_wakeup_defer_race.py api/streaming.py api/gateway_chat.py api/tool_limit_continuation.py
  git commit -m "fix: serialize continuation teardown"
  ```

### Task 6: Verify the original active-thread/activity behavior

**Files:**
- Modify: `api/routes.py:12008-12040` (only if a lineage key reaches health output)
- Test: `tests/test_execution_lineage_admission.py`
- Test: `tests/test_streaming_session_sidebar.py` or the closest existing activity fixture

- [ ] **Step 1: Add a regression for activity cleanup across a physical-ID rotation**

  Rotate an active run from ancestor to compression tip, execute the production deferred-finalizer/unregister order, and assert no stale active activity row or duplicate visible working indicator remains. This validates the UI symptom instead of trusting in-memory registries alone.

- [ ] **Step 2: Run the regression and verify the expected failure if present**

  Run: `./scripts/test.sh tests/test_execution_lineage_admission.py tests/test_streaming_session_sidebar.py -q`

  Expected: the test fails if finalization still clears only the original physical ID.

- [ ] **Step 3: Make the smallest identity-safe cleanup adjustment**

  Use the authoritative entry returned by `unregister_active_run()` for deferred activity finalization, while preserving the release-barrier ordering and the existing lineage key. Do not alter sidebar/archive semantics or add a new activity store.

- [ ] **Step 4: Run the regression and focused neighbors**

  Run: `./scripts/test.sh tests/test_execution_lineage_admission.py tests/test_streaming_session_sidebar.py tests/test_auto_compression_card.py -q`

  Expected: no stale activity row after rotation and all neighboring compression/sidebar tests pass.

### Task 7: Full verification and handoff

**Files:**
- No additional files unless a test exposes a scoped regression.

- [ ] **Step 1: Run the complete focused runtime set**

  ```bash
  ./scripts/test.sh tests/test_execution_lineage_admission.py tests/test_wakeup_defer_race.py tests/test_tool_limit_continuation.py tests/test_release_admission.py tests/test_release_finalizer_barrier.py tests/test_streaming_session_sidebar.py tests/test_auto_compression_card.py
  ```

- [ ] **Step 2: Run runtime lint/static checks**

  Run the repository-prescribed runtime lint command from `TESTING.md` and verify exit 0.

- [ ] **Step 3: Run the broad prescribed test gate**

  Run: `./scripts/test.sh`

  Record exact pass/skip/failure counts. Report the already-known unrelated `tests/test_gateway_sync.py::test_gateway_sessions_appear_when_enabled` failure separately if it remains; do not hide it or attribute it to this patch without evidence.

- [ ] **Step 4: Review the final diff and dirty-work boundary**

  Verify only lineage-fix commits contain the new files/hunks, the 17 pre-existing dirty files remain untouched except for explicitly listed scoped hunks, no secrets or opaque keys appear in public health/log payloads, and `git diff --check` is clean.

- [ ] **Step 5: Commit any final test-only adjustment and report evidence**

  Use one final focused commit only if needed. Include the incident reproduction result, original activity cleanup result, focused output, broad-gate result, and any unverified external-runner limitation in the handoff.
