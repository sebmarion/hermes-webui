# Live Main and Fix-Forward Local Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Run the local WebUI directly from the dirty `main` checkout, make all startup recovery fix-forward, and prevent the WebUI self-update controls from mutating or destroying local work.

**Architecture:** The per-user LaunchAgent points at `bootstrap.py --foreground` in `/Users/seb/hermes-webui` with no selector or release snapshot and no blind crash loop. A live-main policy marker makes update endpoints read-only and hands upstream synchronization to the agent. Persistent tracked/global/local agent instructions define the merge-preserve and retry contract.

**Tech Stack:** Python stdlib WebUI, existing pytest harness via `./scripts/test.sh`, vanilla JavaScript update banner, macOS launchd plist, Codex `AGENTS.md` instructions.

---

### Task 1: Add failing live-main updater policy tests

**Files:**
- Create: `tests/test_live_main_update_policy.py`
- Modify: `tests/test_update_check_ui.py` only for the read-only status marker

- [ ] **Step 1: Write tests** for `HERMES_WEBUI_LIVE_MAIN=1` proving `apply_update`, `apply_force_update`, and `apply_clear_lock` return `agent_merge_required` without invoking Git, and for update-check payloads exposing the live-main mode.
- [ ] **Step 2: Run the focused tests** with `./scripts/test.sh tests/test_live_main_update_policy.py -q` and confirm they fail because the policy marker and response do not yet exist.
- [ ] **Step 3: Run the policy tests** and confirm the strict false-value cases still fail until the implementation exists.

### Task 2: Implement the non-destructive live-main updater boundary

**Files:**
- Modify: `api/updates.py:~290, ~1336, ~1952`
- Modify: `api/routes.py:18086-18115` only if route-level messaging needs adjustment

- [ ] **Step 1: Add `_is_live_main_mode()`** reading `HERMES_WEBUI_LIVE_MAIN` strictly (`1`, `true`, `yes`, or `on`).
- [ ] **Step 2: Make public mutation entrypoints return a structured agent handoff** before any Git, lock, stash, restart, or filesystem mutation when live-main mode is enabled. Preserve target validation and a clear recovery message.
- [ ] **Step 3: Add `live_main: true` to update-check output** in live-main mode so the frontend can render a read-only status.
- [ ] **Step 4: Use one exact handoff schema** for every blocked mutation: HTTP 200 JSON `{ok:false, agent_merge_required:true, target:<target>, message:<actionable text>}`. Add route-level coverage for both `/api/updates/apply` and `/api/updates/force`.
- [ ] **Step 5: Run the new policy tests** and confirm they pass while existing non-live update behavior remains covered.
- [ ] **Step 6: Run the existing update suite** with `./scripts/test.sh tests/test_updates.py tests/test_update_checker.py tests/test_update_stash_recovery.py -q`; update obsolete live-main expectations only, never weaken the no-destruction assertions.

### Task 3: Add the agent-owned no-clobber upstream sync helper

**Files:**
- Create: `scripts/sync_live_main.py`
- Create: `tests/test_sync_live_main.py`
- Modify: `docs/supervisor.md` with the operator-facing invocation and recovery states

- [ ] **Step 1: Write temporary-repository tests** covering staged, unstaged, and untracked local edits; local commits ahead of upstream; fetch followed by a normal merge; and a merge/stash-apply conflict that leaves the named stash recoverable.
- [ ] **Step 2: Run `./scripts/test.sh tests/test_sync_live_main.py -q`** and confirm the tests fail because the helper does not exist.
- [ ] **Step 3: Implement `sync_live_main(repo, upstream_ref='origin/main')` and its CLI** with explicit repo/branch/in-progress-operation validation, `fetch`, a named `stash push --include-untracked` only when needed, `merge --no-rebase`, `stash apply --index` (never `pop`), and human-readable conflict handoff. It must never invoke `reset`, `restore`, `checkout .`, `clean`, or rebase.
- [ ] **Step 4: Keep the stash until the caller explicitly confirms verification**; the helper must print its stash object/name on every non-success path and never silently drop it.
- [ ] **Step 5: Run the helper tests** and verify the full preservation/conflict matrix passes.

### Task 4: Make the update banner read-only in live-main mode

**Files:**
- Modify: `static/index.html:419-422`
- Modify: `static/ui.js:9718-9860`
- Modify: `static/panels.js:11966-12035` only if status text needs a shared helper
- Modify: `tests/test_update_apply_ui.py`

- [ ] **Step 1: Add a failing UI harness assertion** that live-main update data hides `Update Now`, `Force update`, and lock-retry mutation controls and renders an agent-owned merge instruction.
- [ ] **Step 2: Implement the minimal banner change** using the server-provided `live_main` marker; retain read-only comparison/summary links.
- [ ] **Step 3: Ensure stale controls are cleared** when switching from a previous non-live response to a live-main response.
- [ ] **Step 4: Run `./scripts/test.sh tests/test_update_apply_ui.py tests/test_update_check_ui.py -q` and confirm green.

### Task 5: Point the actual LaunchAgent at live `main`

**Files:**
- Modify: `/Users/seb/Library/LaunchAgents/com.parantoux.hermes-webui.plist` (external local runtime state)
- Verify: `bootstrap.py`, `docs/supervisor.md`

- [ ] **Step 1: Verify the target domain/service identity, intended `main` checkout, compatible interpreter, plist ownership, and current PID/listener/source path; save the current plist to an explicit timestamped backup outside the repo and verify it is readable before mutation.
- [ ] **Step 2: Replace selector/snapshot arguments** with `/Users/seb/.hermes/hermes-agent/venv/bin/python /Users/seb/hermes-webui/bootstrap.py --foreground --no-browser`, set `WorkingDirectory` to the checkout, remove selector variables, add `HERMES_WEBUI_LIVE_MAIN=1`, and disable `KeepAlive`.
- [ ] **Step 3: Reload the LaunchAgent** with `launchctl bootout/bootstrap` or the existing safe reload command, then wait for the bounded health check.
- [ ] **Step 4: Prove the running argv/source path**, `/health`, one deep health request, port ownership, and unchanged intentional worktree edits. If reload fails, repair the same checkout and retry; do not restore the plist or worktree as an automatic recovery.

### Task 6: Install durable agent instructions

**Files:**
- Modify: `AGENTS.md` (one generic loader rule only)
- Create: `AGENTS.local.md` (ignored machine-local contract)
- Modify: `/Users/seb/.codex/AGENTS.md` (global Codex contract)
- Add/update: `tests/test_agent_guidance.py` if a lightweight tracked instruction guard is useful

- [ ] **Step 1: Add the generic tracked instruction** to read `AGENTS.local.md` when present and treat it as a local restriction, not expanded authority.
- [ ] **Step 2: Write the local/global contract** covering direct-main runtime, expected dirty worktree, agent-owned upstream merge, named `stash --include-untracked` preservation only for merges, no destructive Git recovery, and fix/retry until health passes.
- [ ] **Step 3: Start a fresh Codex session or equivalent discovery check** and verify both the project and global instructions are read.

### Task 7: Final verification and handoff

**Files:**
- No new production files.

- [ ] **Step 1: Run focused update and bootstrap tests through `./scripts/test.sh`.
- [ ] **Step 2: Run the neighboring runtime/update tests selected by `TESTING.md`.
- [ ] **Step 3: Inspect `git diff` and `git status` to ensure unrelated dirty work is untouched.
- [ ] **Step 4: Verify launchd PID, exact live source path, shallow/deep health, and no selector process/listener remains.
- [ ] **Step 5: Report the external plist change, test commands/results, and any pre-existing failures separately from this change.
