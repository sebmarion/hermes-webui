# Transparent Same-Conversation Compression Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a genuine terminal `compression_exhausted` result into one durable, server-owned continuation in the same Hermes WebUI conversation, while keeping the transcript and composer usable and never creating a focused child session.

**Architecture:** Keep `api/compression_recovery.py` as the bounded seed and recovery-policy owner, add a receipt module that reuses the existing managed-continuation durability primitives, and start the successor through the existing `start_session_turn()` / `_start_run()` path after the parent unregisters. The current session keeps its visible `messages`; only `context_messages` are atomically replaced by the recovery seed. The browser observes the server-owned successor through the existing session channel and renders pending recovery with the existing quiet compression UI.

**Tech Stack:** Python 3.11+, vanilla JavaScript, JSON sidecar receipt store, existing run/turn journals, pytest through `./scripts/test.sh`, Node-based frontend tests already used by the repository.

---

## File map and ownership

- `api/compression_recovery.py`: bounded/redacted seed construction, recovery payloads, blocker policy, and legacy marker recognition.
- `api/compression_recovery_receipts.py`: atomic claim, reservation, launch settlement, human supersession, startup recovery, and managed-store verification.
- `api/streaming.py`: both terminal-result and exception call sites; hidden-control filtering; successful/failed successor settlement; post-parent claim handoff.
- `api/gateway_chat.py`: Gateway terminal settlement and the same post-unregister recovery hook.
- `api/background_process.py`: backend-independent post-unregister successor ordering.
- `api/routes.py`: same-session server-start arguments, old endpoint retirement, startup recovery binding, and human supersession under the session lock.
- `api/models.py`, `api/session_recovery.py`, `api/webui_session_db.py`: persistent recovery phase/control metadata without changing session identity.
- `deferred_release_manifest.py`, `managed_startup_coordinator.py`: managed startup stage and verifier for the new receipt authority.
- `static/messages.js`, `static/ui.js`, `static/style.css`: quiet pending status, same-session stream attachment, enabled composer, and in-place blocker.
- `tests/test_transparent_compression_recovery.py`: seed, receipt, lifecycle, same-session, legacy-adoption, and frontend behavioral tests.
- Existing neighboring suites named below: regression protection for streaming, session lineage, startup, goal/tool continuations, and automatic compression.
- `docs/CONTRACTS.md`, `ARCHITECTURE.md`, `TESTING.md`, `docs/troubleshooting.md`, `docs/UIUX-GUIDE.md`: user/runtime contract updates. `CHANGELOG.md` remains untouched.

## Task 1: Characterize seed policy and build the bounded seed

**Files:**
- Modify: `api/compression_recovery.py`
- Create: `tests/test_transparent_compression_recovery.py`

**Delegated slice:** Run this task only on the local Zeus model
`escha-qwen36-35b-a3b-w2`. Escha may edit exactly the two paths above, may not
commit, and must report the observed RED and GREEN commands. Codex reviews the
diff and owns every later integration task.

- [ ] **Step 1: Write failing seed-policy tests**

Cover these observable cases against a real `Session` object:

```python
seed = build_same_session_recovery_seed(
    session,
    parent_run_id="parent-stream",
    failed_user_text="Ok audit it and do the other steps you said",
    attachments=[{"name": "evidence.txt", "path": "/safe/evidence.txt"}],
    partial_assistant_text="Unverified partial result",
)
assert seed["context_messages"][-1]["role"] == "user"
assert seed["context_messages"][-1]["content"] == failed_user_text
assert seed["fingerprint"]
assert sum(len(str(row.get("content") or "")) for row in seed["context_messages"]) <= RECOVERY_CONTEXT_MAX_CHARS
```

Add separate tests for newest trusted summary, assistant checkpoint fallback for
a short/deictic authorization, independently substantive user text, no-trust
blocker, redaction, bounded partial text, exclusion of
tool/reasoning/error/control rows, exact safe attachment preservation, missing
and conflicting attachment rejection at use, and preservation of the original
visible user row's text, source, timestamp, and attachment metadata.

- [ ] **Step 2: Run the new tests and record the expected RED result**

Run:

```bash
./scripts/test.sh tests/test_transparent_compression_recovery.py -q
```

Expected: collection or assertion failure because `build_same_session_recovery_seed` and its contract do not exist.

- [ ] **Step 3: Implement only the seed policy**

Add a bounded API with this shape:

```python
class CompressionRecoveryBlocked(ValueError):
    def __init__(self, reason: str): ...

def build_same_session_recovery_seed(
    session,
    *,
    parent_run_id: str,
    failed_user_text: str,
    attachments: list[dict] | None = None,
    partial_assistant_text: str = "",
) -> dict:
    """Return JSON-safe context, attachments, trust source, and fingerprint."""
```

Use one assistant reference: newest trusted compressed summary, otherwise the latest substantive assistant checkpoint. Optionally append a clearly labelled bounded partial. Append the exact failed request once as the final user row. Redact assistant-derived text with `_redact_text`; never redact or rewrite the user text. Copy only already-normalized attachment descriptors and compute a SHA-256 fingerprint over canonical JSON plus `(session_id, parent_run_id)`.

- [ ] **Step 4: Run the seed tests and neighboring compression tests**

Run:

```bash
./scripts/test.sh tests/test_transparent_compression_recovery.py tests/test_auto_compression_terminal_failure.py tests/test_auto_compression_card.py -q
```

Expected: seed tests pass; existing ordinary automatic-compression tests remain green.

## Task 2: Add the durable same-session recovery receipt

**Files:**
- Create: `api/compression_recovery_receipts.py`
- Modify: `tests/test_transparent_compression_recovery.py`
- Reference, do not refactor: `api/managed_continuation_recovery.py`, `api/goal_continuation.py`

- [ ] **Step 1: Write failing receipt lifecycle tests**

Test claim-key idempotency, schema/size rejection, atomic persistence before
acceptance, same-session identity, duplicate callbacks, live-owner reservation,
and the full crash matrix:

- dead-owner `reserved` with no submitted successor reclaims;
- dead-owner `launching` with no submitted successor reclaims;
- exact durable `launch_failed` reclaims;
- an exact submitted successor with a matching live or terminal record
  reconciles to `started`; and
- an exact submitted successor without conclusive launch/terminal evidence is
  discarded as ambiguous and never replayed.

Also test successful `started` binding and human supersession.

The required public contract is:

```python
claim_compression_recovery(session, parent_run_id, seed) -> dict
settle_compression_recovery(session_id, parent_run_id, start=None) -> dict | None
recover_pending_compression_recoveries(start=None, session_id=None) -> int
supersede_pending_compression_recovery(session, *, expected_fingerprint=None) -> dict | None
load_receipts() -> dict
```

- [ ] **Step 2: Run and record RED**

Run the receipt-focused node IDs through `./scripts/test.sh`; expected failure is missing receipt APIs.

- [ ] **Step 3: Implement the receipt store using existing durability primitives**

Persist `_compression_recoveries.json` and a private lock beside the other continuation stores. Key receipts by `sha256(session_id + NUL + parent_run_id)`. Store only bounded JSON-safe seed data, normalized profile/source, submitted turn identity when available, fingerprint, state (`claimed`, `starting`, `started`, `discarded`), owner/start tokens, and truthful blocker/discard reason.

Before invoking the starter, reserve the receipt, validate the full
session/profile/fingerprint identity, replace only `session.context_messages`,
attach one structured `_compression_recovery_control`, mark phase `starting`,
and save. Never construct `Session`. Reconcile each dead owner from the exact
turn/run journals: no submitted successor is safe to reclaim even after the
`launching` marker; a matching live/completed successor is already started;
only an exact submitted successor with inconclusive launch/terminal evidence
is ambiguous and discarded.

- [ ] **Step 4: Add managed recovery adapters and validation**

Expose:

```python
recover_managed_compression_recoveries_exact(...)
verify_managed_compression_recoveries_exact(...)
```

using `recover_exact`, `verify_exact`, `stable_store_snapshot`, `strict_store_lock`, and `strict_store_save`. Validate every field and total store bound before any mutation.

- [ ] **Step 5: Run receipt and managed-continuation tests**

```bash
./scripts/test.sh tests/test_transparent_compression_recovery.py tests/test_managed_continuation_recovery.py -q
```

Expected: all pass.

## Task 3: Claim terminal exhaustion and start one successor after unregister

**Files:**
- Modify: `api/streaming.py`
- Modify: `api/gateway_chat.py`
- Modify: `api/background_process.py`
- Modify: `api/routes.py`
- Modify: `api/models.py`
- Modify: `api/session_recovery.py`
- Modify: `api/webui_session_db.py`
- Modify: `tests/test_transparent_compression_recovery.py`
- Modify: `tests/test_compression_recovery_action.py`

- [ ] **Step 1: Write failing integration tests for both terminal paths**

Prove native structured-result, native exception, and Gateway terminal
`compression_exhausted` all call one shared claim helper; the claim is durable
before the parent terminal frame; the failed user row remains exactly once; no
focused child is created; and the successor is not admitted until `STREAMS`
and `ACTIVE_RUNS` no longer contain the parent.

Add a same-session starter assertion:

```python
assert started["session_id"] == original_session_id
assert started["source"] == "compression_recovery"
assert created_session_ids == []
assert session.messages == visible_before_start
assert session.context_messages == receipt_seed
```

Also cover a recovery successor that itself exhausts: it writes one in-place blocker and creates no second claim.

Add a core-contract regression in which the Agent reports the prune-only
diagnostic without structured `compression_exhausted`; WebUI must treat it as
ordinary automatic-compression progress and create zero recovery claims.

- [ ] **Step 2: Run and record RED**

Run only the new integration node IDs. Expected: current code stamps `start_focused_continuation`, blocks sends, and never starts a same-session successor.

- [ ] **Step 3: Centralize terminal settlement**

In `api/streaming.py`, replace both direct
`stamp_compression_exhausted_recovery()` branches with one helper that
materializes the exact pending user row, captures normalized attachments and
safe partial assistant text, builds/saves the receipt, then returns either:

- pending payload: `terminal_state=compression_exhausted`, `phase=claimed`, `automatic_recovery=True`, claim identity, same session ID; or
- blocked payload: `phase=blocked`, a truthful bounded diagnostic, composer-writable state.

Do not persist the red terminal assistant row for an accepted automatic claim. Keep it only for a blocker. Add `compression_recovery` to the internal-control map so its hidden prompt never enters visible `messages` or title generation. On successful recovery settlement clear session recovery metadata; on recovery-source exhaustion mark blocked and do not claim again.

If a recovery successor ends in an ordinary provider, authentication, model,
guardrail, cancellation, or interruption failure, clear the pending recovery
presentation and preserve that ordinary truthful terminal result. Do not
reinterpret it as recovery success and do not create another recovery claim.

Call that same helper from `api/gateway_chat.py` terminal settlement before its
pending fields are cleared. Gateway must emit the same payload/phase and run
the same post-unregister recovery hook; it must not depend on the native
streaming branch to stamp or start recovery.

- [ ] **Step 4: Add compression recovery first in the post-unregister order**

Pass the exact `parent_run_id` into
`recover_successors_after_unregister()` from native and Gateway teardown, then
update it to return and enforce:

```python
{"compression": n, "tool_limit": n, "goal": n, "deferred": n}
```

Compression gets first opportunity for the exact parent claim, followed by existing tool-limit, goal, and deferred wakeup behavior. Preserve all existing backends and lineage checks.

- [ ] **Step 5: Extend the shared server-start path**

Extend `start_session_turn()` with internal-only `attachments`, `recovery_claim_token`, and `recovery_fingerprint` keyword arguments and pass them to `_start_run()`. Treat `source="compression_recovery"` as a hidden internal control in `_prepare_chat_start_session_for_stream()`, active-stream race checks, streaming context canonicalization, and title/display suppression. Continue to route native and Gateway-backed turns through the same `_start_run()` selection.

- [ ] **Step 6: Make human sends supersede a still-claimed recovery**

Remove `_compression_recovery_required_payload()` as a send gate from both chat entry points. While holding the same per-session lock used to commit the new stream, atomically consume a matching claimed receipt, install its seed, mark it `discarded/superseded_by_user`, clear the legacy read-only action, and then start the human message. A started recovery still uses the normal active-stream response; it is never duplicated.

- [ ] **Step 7: Run backend and sibling suites**

```bash
./scripts/test.sh \
  tests/test_transparent_compression_recovery.py \
  tests/test_compression_recovery_action.py \
  tests/test_auto_compression_terminal_failure.py \
  tests/test_goal_command_webui.py \
  tests/test_tool_limit_continuation.py \
  tests/test_wakeup_defer_race.py \
  tests/test_optionz_liveview_perf.py \
  tests/test_start_session_turn_runtime_adapter.py \
  tests/test_webui_gateway_chat_backend.py -q
```

Expected: all pass.

## Task 4: Recover safe legacy markers and bind managed startup

**Files:**
- Modify: `api/compression_recovery_receipts.py`
- Modify: `api/routes.py`
- Modify: `deferred_release_manifest.py`
- Modify: `managed_startup_coordinator.py`
- Modify: `tests/test_transparent_compression_recovery.py`
- Modify: `tests/test_startup_release_fence.py`
- Modify: `tests/test_managed_continuation_recovery.py`
- Modify: `tests/test_release_finalizer_barrier.py`

- [ ] **Step 1: Write failing legacy-adoption and startup tests**

Cover legacy marker with no child, empty child, substantive child, profile mismatch, missing terminal request, unsafe seed, duplicate startup passes, and managed manifest/verifier ordering. A safe legacy marker adopts once; a substantive child is never merged or replayed, but the source composer is no longer globally disabled.

Independently test the first full `GET /api/session` load after deployment: it
must schedule `_maybe_adopt_legacy_compression_recovery_on_session_load()` once
for an eligible marker, and repeated loads/tabs must not create a second claim.

- [ ] **Step 2: Run and record RED**

Run the new legacy/startup node IDs and confirm the current startup descriptors do not include compression recovery.

- [ ] **Step 3: Implement bounded existing-state adoption**

Use index metadata to identify legacy `recommended_action=start_focused_continuation` records, then load full sessions only for bounded candidates. Validate the exact profile/session/user boundary and use `find_compression_recovery_session()` only to detect old children. Adopt when no child has substantive work; otherwise stamp a truthful legacy blocker without merging or repeating work. Do not delete empty child sessions.

Wire the same adoption helper into the full-session branch of `GET
/api/session` after profile/session identity has been validated. The hot
metadata-only path remains read-only; the full-load hook only schedules the
server-owned, idempotent receipt transition and never launches directly from
the request handler.

- [ ] **Step 4: Add the managed startup stage**

Place `compression_recovery` before tool-limit/goal continuation replay in the deferred release manifest and coordinator. Require the new exact mutator and independent verifier, reuse the existing managed-continuation receipt codec, and include the stage in safe-partial and rerun-if-absent policies.

- [ ] **Step 5: Run startup and recovery suites**

```bash
./scripts/test.sh \
  tests/test_transparent_compression_recovery.py \
  tests/test_managed_continuation_recovery.py \
  tests/test_startup_release_fence.py \
  tests/test_release_finalizer_barrier.py \
  tests/test_managed_startup_session_recovery.py -q
```

Expected: all pass.

## Task 5: Replace the manual fork/read-only UI with quiet in-place recovery

**Files:**
- Modify: `static/messages.js`
- Modify: `static/ui.js`
- Modify: `static/style.css`
- Modify: `tests/test_transparent_compression_recovery.py`
- Modify: `tests/test_compression_recovery_action.py`
- Modify: `tests/test_auto_compression_card.py`
- Modify: `tests/test_mobile_reload_compression_recovery.py`
- Modify: `tests/test_hidden_tab_server_initiated_turn.py`
- Modify: `tests/test_session_channel_option_x.py`

- [ ] **Step 1: Write failing real-JavaScript behavior tests**

Assert that a claimed terminal frame does not append a red error assistant row, sets the existing compression UI to `Recovering context...`, leaves the composer enabled, and attaches exactly once when `server_turn_started` arrives. Assert that reload derives pending/running state from session metadata, success clears it, and blocked recovery shows one same-conversation diagnostic with no button, redirect, URL mutation, or sidebar creation.

- [ ] **Step 2: Run and record RED**

Current code must fail because it renders `Start focused continuation`, intercepts every send, and posts to `/api/session/compression-recovery/start`.

- [ ] **Step 3: Remove the active manual-fork contract**

Remove send interception, the focused-continuation action, and active use of the old endpoint. Keep the old endpoint server-side only as a typed `409/reload_required` response for cached clients. Do not remove or rewrite historical focused-child metadata.

- [ ] **Step 4: Render automatic and blocked states**

For `phase in {claimed, starting, running}`, reuse the existing transient compression-status surface with `Recovering context...`. Do not persist a terminal card. For `phase=blocked`, render a concise diagnostic in place, without a button, and leave the composer enabled. The existing session-channel `server_turn_started` handler remains the only browser attachment mechanism.

- [ ] **Step 5: Run frontend and responsive suites**

```bash
./scripts/test.sh \
  tests/test_transparent_compression_recovery.py \
  tests/test_compression_recovery_action.py \
  tests/test_auto_compression_card.py \
  tests/test_mobile_reload_compression_recovery.py \
  tests/test_optionz_liveview_perf.py \
  tests/test_hidden_tab_server_initiated_turn.py \
  tests/test_session_channel_option_x.py -q
```

Expected: all pass with no old manual-action strings in the active UI path.

## Task 6: Update contracts and verify the entire change

**Files:**
- Modify: `docs/CONTRACTS.md`
- Modify: `ARCHITECTURE.md`
- Modify: `TESTING.md`
- Modify: `docs/troubleshooting.md`
- Modify: `docs/UIUX-GUIDE.md`
- Modify: the linked stable-turn/runtime RFC sections that currently describe
  terminal `compression_exhausted` as a user-owned recovery boundary
- Do not modify: `CHANGELOG.md`

- [ ] **Step 1: Update documentation**

Document the distinction between ordinary Agent auto-compression, terminal
same-conversation recovery, visible transcript ownership, model-context
replacement, receipt/startup ownership, blocker behavior, and the retired
cached-client endpoint. Update the exact affected RFCs:

- `docs/rfcs/live-to-final-assistant-replies.md`
- `docs/rfcs/webui-run-state-consistency-contract.md`
- `docs/rfcs/turn-journal.md`
- `docs/rfcs/stable-assistant-turn-anchors.md`

- [ ] **Step 2: Run targeted and neighboring verification fresh**

Run all commands from Tasks 1–5 again after documentation and refactoring changes.

Run `npm run lint:runtime` after JavaScript edits and fix every reported error.

- [ ] **Step 3: Run the repository-required full test command**

```bash
./scripts/test.sh
```

Expected: zero failures. If the full suite has unrelated pre-existing failures, preserve the complete output, prove targeted suites are green, and stop before commit/push until the user decides.

- [ ] **Step 4: Inspect scope and obtain two-stage review**

Verify `git diff --name-only`, `git diff --check`, and the complete diff. Confirm unrelated `api/config.py`, `tests/test_issue1240_generic_cli_catalog_sync.py`, and `default-home/` work is untouched. Obtain spec-compliance review first, then code-quality review; fix every Critical or Important issue and rerun affected tests.

- [ ] **Step 5: Restart the live-main runtime and verify health**

Use the checked-in live-main restart path from `AGENTS.local.md`. Prove `/health`, deep health, PID/source identity, and `/Users/seb/hermes-webui` runtime binding. Use isolated state/fixtures for exhaustion and recovery behavior; do not mutate real session history to manufacture a failure.

Capture isolated live evidence for both required fixtures: a trusted-summary
exhaustion and the screenshot-shaped prior-assistant-plan plus short
authorization. For each, record unchanged session ID, title, URL, sidebar row,
and visible transcript while the successor streams. Capture before/after
desktop, narrow, and mobile screenshots proving the composer remains enabled,
pending recovery is quiet, and no focused-continuation control appears.
Restart/reload during a claimed fixture and issue duplicate load/start probes;
record that the exact receipt reconciles once, one successor stream exists,
and no repeated tool/model turn is admitted.

- [ ] **Step 6: Commit and push only the scoped files**

Stage explicit task paths, commit on the existing `main`, confirm the commit excludes unrelated dirty/untracked work, then:

```bash
git push sebmarion main
```

Record the final commit, remote update, targeted/full test evidence, and live health receipt.
