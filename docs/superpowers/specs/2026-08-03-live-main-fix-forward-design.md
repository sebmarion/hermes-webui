# Live Main and Fix-Forward Local Runtime Design

## Goal

Make the local Hermes WebUI iterate directly from the checked-out `main`
branch, removing the release-selector/cutover ceremony while ensuring startup
failures and upstream updates never destroy local work.

## Context

This checkout is a single-user local runtime. The current LaunchAgent starts an
immutable release selected by a release controller, while the WebUI self-update
path has a destructive force-update fallback (`checkout`, `clean`, and
`reset --hard`). That model adds operational state and makes a local iteration
failure harder to repair than the code problem itself.

## Design

### Runtime ownership

The LaunchAgent will start the live checkout at
`/Users/seb/hermes-webui` directly, using the supported foreground bootstrap
entrypoint and the known compatible Hermes-agent interpreter. The normal plist
will run at load but will not keep respawning a broken process. The live source
path is part of the health proof.

Release snapshots, selector state, candidate cutover, promotion, retention,
and rollback are outside the normal local path. Existing release assets are
left dormant initially; cleanup is a separate task.

### Fix-forward startup loop

An agent owns the edit/test/restart loop:

1. Verify the checkout and branch are the intended live `main` worktree.
2. Run the affected tests through `./scripts/test.sh`.
3. Restart the LaunchAgent once and prove both shallow and deep health.
4. If startup or health fails, inspect logs and source identity, repair the
   same worktree, and retry until healthy.

Startup failure never authorizes `git reset`, `git restore`, `git checkout` to
discard work, `git clean`, selector rollback, or force-update.

### Upstream synchronization

The WebUI may report that upstream updates exist, but it will not mutate its
own source tree. An agent performs the merge:

1. Record the current branch, `HEAD`, staged/unstaged status, and untracked
   files; fetch the configured upstream.
2. If local changes would obstruct the merge, create a named safety stash that
   includes staged and untracked files and record its object ID. This is a
   temporary preservation envelope for an explicit upstream merge only.
3. Merge upstream into local `main` with a normal merge. Never rebase, reset,
   clean, or replace the worktree with a release snapshot.
4. Reapply the safety stash with index state using `stash apply`, never `stash
   pop`. Resolve conflicts while retaining the stash as a recovery copy.
5. Run tests, restart the live process, and verify health/source identity.
6. Drop the safety stash only after the merged local edits are present and the
   verification succeeds; otherwise leave it recoverable and continue fixing.

The agent may resolve ordinary code conflicts using the current requirements.
An unresolved product decision is the only reason to pause for the user.

### Agent enforcement

The tracked root `AGENTS.md` will contain only a generic instruction to read
`AGENTS.local.md` when present. The ignored local file and the global Codex
instructions will state the live-main/fix-forward contract, including the
prohibition on destructive Git recovery and the required health proof. A fresh
Codex session will be used to verify discovery.

### Self-update behavior

The destructive Force Update path is removed. The ordinary self-update action
becomes read-only status (or an explicit handoff to the agent); it cannot pull,
reset, clean, or overwrite the live checkout. Upstream merge is an agent-owned
operation so the running process is never left serving a half-applied merge.

## Error handling

- A failed fetch or merge leaves the worktree and any safety stash intact and
  reports the exact next repair step.
- A merge or stash-apply conflict is repaired in place; no rollback is used to
  make the process green.
- A failed restart preserves the failed tree for diagnosis. The agent retries
  after each repair and stops only when the source path and health checks pass.
- Durable state migrations remain separately approval-gated; code changes still
  use fix-forward semantics.

## Verification

Tests will cover the updater's non-destructive behavior and the merge-preserve
contract using temporary Git repositories containing staged, unstaged,
untracked, and diverged local work. Runtime verification will prove that the
LaunchAgent argv points at the live checkout, that `/health` is healthy, and
that the worktree remains intact after a failed startup and after a successful
upstream merge.

## Non-goals

- Rebuilding the release controller or selector.
- Deleting existing release snapshots in this change.
- Automatically committing the user's work-in-progress edits.
- Building a multi-machine deployment or public release pipeline.
