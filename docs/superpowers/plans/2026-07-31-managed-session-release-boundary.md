# Managed Session Release Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep managed WebUI cutovers independent of production transcript/database size while retaining exact path, owner, mode, symlink, transaction, manifest, and replay checks.

**Architecture:** Preserve `api/managed_startup_session_recovery.py` as the explicit deep diagnostic. Add a separate managed-release boundary that performs no recovery and reads no session/database payload bytes; it binds canonical owner-private directory and SQLite bundle identities into a small immutable receipt. Route only the fenced managed startup path and its durable coordinator through that receipt. Unmanaged startup keeps its existing recovery behavior.

**Tech Stack:** Python 3.11, frozen dataclass receipts, `os.open`/`fstat` no-follow identity checks, existing managed startup coordinator, pytest via `./scripts/test.sh`.

**Design source:** `docs/superpowers/specs/2026-07-31-zeus-evidence-calibrated-webui-release-design.md` requires isolated smoke and exact shallow cutover health, and excludes deep production health from activation.

---

### Task 1: Specify the no-payload-read boundary

**Files:**
- Create: `tests/test_managed_startup_session_boundary.py`

- [ ] **Step 1: Write the failing large-state regression test**

Create owner-private temporary session and state directories, a sparse session sidecar larger than the old 8 MiB cap, and a sparse `state.db` larger than the old 512 MiB aggregate cap. Assert `attest_managed_startup_session_boundary(...)` and `verify_managed_startup_session_boundary(...)` complete without calling `os.read`.

- [ ] **Step 2: Write fail-closed identity tests**

Assert that a symlinked directory, a non-private database file, and a database identity change after receipt creation return an error or `AMBIGUOUS` verification.

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
./scripts/test.sh tests/test_managed_startup_session_boundary.py -q
```

Expected: collection fails because `api.managed_startup_session_boundary` does not yet exist.

### Task 2: Implement the structural receipt

**Files:**
- Create: `api/managed_startup_session_boundary.py`
- Test: `tests/test_managed_startup_session_boundary.py`

- [ ] **Step 1: Add immutable receipt and verification types**

Define a frozen receipt containing the exact transaction and deferred-manifest binding, canonical session directory path and held identity, canonical state database path, exact main/WAL/SHM file identities, and a canonical evidence digest. Reuse `SessionRecoveryOutcome` so coordinator reconciliation remains exact.

- [ ] **Step 2: Hold paths without reading payloads**

Walk every directory component using `O_NOFOLLOW`; require the final session directory and state database parent to be owned by the current user with mode `0700`. Open present database bundle members with `O_NOFOLLOW|O_NONBLOCK`; require regular, single-link, current-user, mode-`0600` files and compare pathname/fd identities before and after. Do not enumerate or parse session payloads, serialize SQLite, or apply byte/count thresholds.

- [ ] **Step 3: Verify an exact fresh observation**

Re-attest using the receipt paths and requested binding. Return `AMBIGUOUS` for missing receipts, unsafe paths, identity changes, or binding drift; return `PROVED_COMPLETE` only for exact equality.

- [ ] **Step 4: Run the new tests and verify GREEN**

Run:

```bash
./scripts/test.sh tests/test_managed_startup_session_boundary.py -q
```

Expected: all tests pass.

### Task 3: Route managed startup only through the boundary

**Files:**
- Modify: `server.py`
- Modify: `managed_startup_coordinator.py`
- Modify: `tests/test_managed_startup_session_recovery.py`
- Test: `tests/test_managed_startup_coordinator.py`
- Test: `tests/test_startup_release_fence.py`

- [ ] **Step 1: Write/adjust failing routing assertions**

Assert unmanaged startup still invokes `recover_all_sessions_on_startup`, while a fenced managed release invokes only `attest_managed_startup_session_boundary`. Assert reconciliation calls the matching verifier and retains the immutable receipt.

- [ ] **Step 2: Update server and coordinator wiring**

Replace only managed-session imports/calls. Register the new frozen receipt under `webui.session-boundary-receipt.v1`; retain the existing outcome codec. Do not change the deferred step name or ordering.

- [ ] **Step 3: Run focused release/startup tests**

Run:

```bash
./scripts/test.sh \
  tests/test_managed_startup_session_boundary.py \
  tests/test_managed_startup_session_recovery.py \
  tests/test_managed_startup_coordinator.py \
  tests/test_startup_release_fence.py -q
```

Expected: all selected tests pass.

### Task 4: Verify, publish, and promote

**Files:**
- Verify only; no additional source files unless a test exposes a class-level defect.

- [ ] **Step 1: Run neighboring managed-release suites**

Run the existing release-control, deferred-startup, selector, and release-cutover tests through `./scripts/test.sh`.

- [ ] **Step 2: Run a read-only live-state preflight**

Invoke the new boundary against `/Users/seb/.hermes/webui/sessions` and `/Users/seb/.hermes/state.db`; require `PROVED_COMPLETE` without reading the 6.3 GiB transcript corpus or 5.4 GiB database payload.

- [ ] **Step 3: Commit and push**

Commit one logical bugfix, push the exact commit to `sebmarion/main`, and verify the remote ref resolves to that commit.

- [ ] **Step 4: Build and promote a new immutable candidate**

Build a new release ID from the pushed commit. Use a fresh selector transaction/generation, retain r90 as last-good, checkpoint active tasks, activate under the existing fence, require exact shallow health, promote by selector CAS, and reopen only after exact accepted identity is re-read.

- [ ] **Step 5: Verify terminal live state**

Require selector `current == last_good == <new candidate>`, `candidate == null`, no pending transaction, exact `/health` build/commit/tree/manifest, admission open, one stable LaunchAgent process, Gateway healthy with zero work, and remote `main` at the same commit.
