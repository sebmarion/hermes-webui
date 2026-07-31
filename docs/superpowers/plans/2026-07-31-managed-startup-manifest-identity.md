# Managed Startup Manifest Identity Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make transaction-fenced WebUI candidates pass startup acceptance only when both the immutable package identity and the canonical deferred-startup manifest identity are independently verified.

**Architecture:** Keep `HERMES_WEBUI_MANIFEST_SHA256` as the package manifest hash supplied by the selector. Have the immutable release bootstrap compute and export `HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256` for startup-fenced launches. Change the startup coordinator and managed session-recovery binding to consume only the dedicated deferred hash, preserving fail-closed behavior and existing package health attestation.

**Tech Stack:** Python 3.11-3.13, pytest through `./scripts/test.sh`, vanilla WebUI bootstrap, immutable release selector/cutover tooling.

---

### Task 1: Prove the overloaded binding fails with two distinct hashes

**Files:**
- Modify: `tests/test_managed_startup_coordinator.py`

- [ ] **Step 1: Write the failing regression test**

Add a test that supplies a valid package hash in `HERMES_WEBUI_MANIFEST_SHA256` and the real `deferred_release_manifest.deferred_release_manifest_sha256()` in `HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256`, then asserts `build_managed_startup_coordinator()` succeeds and its receipt uses the deferred hash.

- [ ] **Step 2: Run the test to verify it fails for the current defect**

Run:

```bash
./scripts/test.sh tests/test_managed_startup_coordinator.py -k distinct -q
```

Expected: FAIL because current coordinator compares the package hash against the deferred manifest hash.

- [ ] **Step 3: Commit the red test**

```bash
git add tests/test_managed_startup_coordinator.py
git commit -m "test: expose managed manifest identity collision"
```

### Task 2: Add the bootstrap-owned deferred-manifest binding

**Files:**
- Modify: `bootstrap.py`
- Test: `tests/test_bootstrap_foreground.py`

- [ ] **Step 1: Add bootstrap tests for the producer contract**

Cover a startup-fenced managed environment with a package hash and assert that the helper preserves the package hash while exporting the canonical deferred hash. Add a conflicting pre-supplied deferred hash case that raises before `server.py` execution. Include the new variable in the test environment cleanup fixture.

- [ ] **Step 2: Run the focused bootstrap tests before implementation**

Run:

```bash
./scripts/test.sh tests/test_bootstrap_foreground.py -k deferred_manifest -q
```

Expected: FAIL because the helper does not yet exist.

- [ ] **Step 3: Implement the minimal helper and call site**

Add a private bootstrap helper that:

1. returns without action for an unfenced launch;
2. imports the sealed release's `deferred_release_manifest` only after managed package validation;
3. computes `hashlib.sha256(deferred_release_manifest.canonical_manifest_bytes(deferred_release_manifest.deferred_release_manifest())).hexdigest()` and validates the result as lowercase 64-character hexadecimal, matching the server's existing canonical procedure byte-for-byte;
4. rejects a present conflicting `HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256`; and
5. exports the canonical value before the `execv`/child-server path.

Call it immediately after `_managed_bootstrap_python()` succeeds. Do not alter `HERMES_WEBUI_MANIFEST_SHA256`.

- [ ] **Step 4: Run the focused bootstrap tests**

Run the same command and expect PASS.

- [ ] **Step 5: Commit the producer**

```bash
git add bootstrap.py tests/test_bootstrap_foreground.py
git commit -m "fix: bind deferred startup manifest separately"
```

### Task 3: Switch coordinator and session recovery to the dedicated hash

**Files:**
- Modify: `managed_startup_coordinator.py`
- Modify: `server.py`
- Modify: `tests/test_managed_startup_coordinator.py`
- Modify: `tests/test_managed_startup_session_recovery.py`

- [ ] **Step 1: Update coordinator tests**

Change managed startup fixtures to provide a package hash and the canonical dedicated deferred hash. Add missing/wrong dedicated-hash cases and assert the package hash may differ without weakening the package identity contract. Audit every `HERMES_WEBUI_MANIFEST_SHA256` startup-fence consumer with `rg`; update only deferred-startup consumers, while leaving build identity, selector, health, and admission package-identity consumers on the package key. Cover both process-environment bindings and the in-process configuration binding used by recovery.

- [ ] **Step 2: Update session-recovery tests**

Change the managed session-recovery fixtures to bind the dedicated hash. Add a case proving a canonical package hash with a missing or wrong dedicated hash is rejected before the managed recovery audit is called.

- [ ] **Step 3: Implement the consumer changes**

In both coordinator construction and `_managed_startup_session_binding()`, read and validate only `HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256` against the canonical deferred manifest hash. Keep the package hash available to build identity, health, selector, and admission code unchanged. Ensure rejection happens before any managed recovery mutator, audit call, cleanup, or bookkeeping write.

- [ ] **Step 4: Run focused tests**

```bash
./scripts/test.sh tests/test_managed_startup_coordinator.py tests/test_managed_startup_session_recovery.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the consumers and tests**

```bash
git add managed_startup_coordinator.py server.py tests/test_managed_startup_coordinator.py tests/test_managed_startup_session_recovery.py
git commit -m "fix: separate deferred startup manifest validation"
```

### Task 4: Verify selector and direct-fallback package identity behavior

**Files:**
- Modify: `tests/test_bootstrap_foreground.py`
- Modify: `tests/test_webui_release_selector.py` only if the existing assertions need the new bootstrap boundary represented
- Modify: `tests/test_startup_release_fence.py` only where managed environment fixtures must include the dedicated key

- [ ] **Step 1: Add both launch-path assertions**

Assert selector and direct-fallback environments continue to carry the immutable package hash unchanged. Test that the bootstrap derives the same dedicated deferred hash for both paths, and that a conflicting supplied value is rejected in each path.

- [ ] **Step 2: Run neighboring release tests**

```bash
./scripts/test.sh tests/test_webui_release_selector.py tests/test_startup_release_fence.py tests/test_bootstrap_foreground.py -q
```

Expected: PASS with no package-identity regressions.

- [ ] **Step 3: Commit the launch-path coverage**

```bash
git add tests/test_webui_release_selector.py tests/test_startup_release_fence.py tests/test_bootstrap_foreground.py
git commit -m "test: cover deferred manifest across managed launch paths"
```

### Task 5: Full verification and immutable release preparation

**Files:**
- No additional source files unless a test exposes a directly related sibling contract.

- [ ] **Step 1: Run the complete repository test runner**

```bash
./scripts/test.sh
```

Expected: PASS.

- [ ] **Step 2: Inspect the final diff and immutable source identity**

Verify the release worktree is clean except for the intended commits, the source commit is pushed to the configured main remote, and no selector state or sealed r97 artifact was edited.

- [ ] **Step 3: Build a fresh immutable candidate**

Use the existing sealed release-preparation tooling with a new transaction/build identity. Before any force-enabled action, prove read-only that the candidate build directory and transaction journal are new, r97 remains immutable, selector `current` is still r90 with `candidate` and `pending_transaction_id` null, and no r97 path is selected. The candidate must contain the bootstrap, coordinator, server, and test changes from this worktree and must not reuse r97's immutable artifact or terminal journal.

- [ ] **Step 4: Execute the guarded cutover**

Use the existing cutover actuator with the bounded launchd force-restart behavior only after the fresh-candidate preflight passes. The actuator may mutate only its new transaction's staged/rollback paths and selector transitions; it must fail closed and use its sealed rollback receipt on any error. Verify signed startup-fenced health, successful deferred startup acceptance, paired Gateway open, final selector state, and rollback receipts.

- [ ] **Step 5: Verify production state**

Confirm the WebUI and Gateway report the same new release pair, WebUI admission is open, Gateway admission is accepting, process completion queue depth is zero, candidate and pending transaction are null, and the paused peer tasks remain paused until promotion is terminal.
