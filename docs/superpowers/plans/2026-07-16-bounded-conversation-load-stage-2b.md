# Bounded Conversation Load Stage 2B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a complete initial conversation view eligible for the bounded cursor path using proof-based reconciliation, crash-safe derived state, and bounded runtime overlays.

**Architecture:** WebUI-owned per-lineage receipt and todo projection files live under the WebUI state directory and are atomically replaceable/rebuildable. A focused assembler validates all proof first, chooses cursor or exact one-conversation legacy mode once, overlays only proven active runtime state, and redacts once; `state.db` remains canonical.

**Tech Stack:** Python, atomic JSON files, SQLite watermarks, existing Session/run-journal/todo machinery, pytest through `./scripts/test.sh`.

---

## File structure

- Modify `api/models.py`: persisted monotonic `sidecar_generation` and the one shared sidecar-write critical section.
- Modify `api/session_recovery.py`: generation-aware backup restore and state-db materialization.
- Create `api/conversation_receipts.py`: hashed per-profile/root receipt storage and validation.
- Create `api/conversation_view_state.py`: atomic todo projection and CAS.
- Create `api/conversation_shadow_evidence.py`: durable zero-diff enablement evidence and latched disablement.
- Create `api/bounded_runtime_overlay.py`: one-run bounded journal/in-memory overlay.
- Create `api/bounded_session_view.py`: proof-first initial-view assembler.
- Modify `api/routes.py`, `api/streaming.py`, and `api/todo_state.py`: narrow integration hooks.
- Modify `scripts/benchmark_conversation_load.py`: complete-view SLO/mechanical gate.
- Add focused receipt, projection, runtime, and route tests.

## Impact gate

`Session.save`, `handle_get`, and the legacy merge oracle are manually HIGH risk; do not alter `merge_session_messages_append_only`. Every `Session.save` change must preserve failed-replace semantics and 112 callers. Todo and runtime helpers are MEDIUM risk. Receipt/projection writes ship before reads; all read gates default off.

### Task 1: Monotonic sidecar generation

**Files:**
- Modify: `api/models.py:1089-1434`
- Modify: `api/session_recovery.py:343-371, 592-652`
- Create: `tests/test_session_sidecar_generation.py`

- [ ] **Step 1: Write failing persistence tests**

```python
s = Session(session_id="s", messages=[])
s.save()
first = Session.load("s").sidecar_generation
s.save(touch_updated_at=False)
assert Session.load("s").sidecar_generation == first + 1
```

Cover legacy missing field -> zero, atomic replace failure leaves disk/in-memory generation unchanged, metadata-only load sees the value, and truncation/edit saves advance it without changing recency rules. Load two stale `Session` objects, save them concurrently, and require two distinct monotonic generations. Cover `recover_session()` replacing a live sidecar from an older `.bak`, `recover_missing_sidecars_from_state_db()`, a recovery/save race, and index-write failure after the sidecar replace.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_session_sidecar_generation.py -q`

Expected: FAIL because the field does not exist.

- [ ] **Step 3: Implement one generation-aware sidecar critical section**

Add a bounded process-local set of striped `threading.RLock` sidecar-write locks, selected by safe session ID, and a shared helper that reads only the metadata prefix under that lock, computes `next_generation = max(persisted_generation, object_generation, 0) + 1`, injects it into the payload, fsyncs, and performs the atomic replace/create. `Session.save()` must always use the helper, including callers that already hold the non-reentrant Agent session lock. Update `self.sidecar_generation` immediately after the sidecar replace succeeds; a later index failure must not make the object claim the old disk generation. A failed sidecar replace updates neither disk nor object.

Place `sidecar_generation` in the metadata prefix before `messages` and make `load_metadata_only()` parse it. Do not full-parse the previous sidecar to allocate a generation.

- [ ] **Step 4: Route every direct session-sidecar writer through the same helper**

Change `recover_session()` so it parses the chosen backup payload, allocates a generation newer than both the live and backup generation under the same sidecar lock, then atomically replaces the live file. Change `recover_missing_sidecars_from_state_db()` so create-or-fail writes generation 1 under that lock without overwriting a concurrent live save. Truncation, clear, retry, undo, edits, compression, and stream checkpoints remain covered centrally because they call `Session.save()`.

Add a static audit test that searches production Python for direct writes/replaces of `<SESSION_DIR>/<sid>.json` or `.json.bak`; each hit must be `Session.save()`, the shared generation-aware helper, or a documented non-session artifact allowlist (index, tombstone, journal). This prevents a future writer from bypassing generation invalidation.

- [ ] **Step 5: Run GREEN and save/truncation/recovery regressions**

Run: `./scripts/test.sh tests/test_session_sidecar_generation.py tests/test_atomic_writer_fsync.py tests/test_session_duplicate_edit.py tests/test_truncate_session_at_keep.py tests/test_session_truncate_keep_count_validation.py tests/test_session_db_sidecar_reconciliation.py tests/test_issue5532_session_clear_state_db_replay.py tests/test_issue5570_clear_backup_recovery.py -q`

Expected: PASS.

- [ ] **Step 6: Detect changes and commit**

Commit: `feat: version session sidecar snapshots`

### Task 2: Atomic reconciliation receipts

**Files:**
- Create: `api/conversation_receipts.py`
- Create: `tests/test_conversation_reconciliation_receipts.py`

- [ ] **Step 1: Write failing store/validation tests**

Define a receipt with no content:

```python
receipt = ConversationReceipt(
    version=1,
    profile="default",
    root_id="root",
    member_ids=("root", "tip"),
    lineage_fingerprint="fp",
    sidecar_generation=4,
    sidecar_stat=stat_signature,
    truncation_watermark=12.5,
    state_message_watermark=(901, 123.0),
    state_content_proof=(
        "agent_target_content_epoch_v1",
        (("root", 42), ("tip", 7)),
    ),
    settled_display_message_count=88,
    visible_transcript_digest="sha256:" + ("0" * 64),
    generation=3,
)
```

Test profile/root isolation, safe hashed filenames, atomic replace/fsync, corrupt/version-mismatch reads, stat/generation/truncation/watermark/count mismatch, and unrelated global projection-generation changes.

The bounded validator must require a declared, target-scoped Agent message-content proof: a bounded ordered vector of `(resolved_member_id, message_generation)` plus the lineage fingerprint. Every member generation must change transactionally for every insert, update, delete, session-ID move, active-state change, truncation, or compaction affecting that member. Count/max-ID/max-timestamp, filesystem stat, SQLite `data_version`, and global projection generation are hints only and must never be promoted to this proof. Capability detection trusts only an explicitly versioned Agent contract backed by Agent-owned triggers/write semantics, not a coincidentally named column. WebUI does not add a table, trigger, index, or migration.

On the current/any schema without that declared proof, validation returns `unverifiable_current_state` and the request stays exact legacy. Add fixtures that (a) change interior content while preserving row ID, timestamp, count, session metadata, and watermarks and (b) toggle an interior row's `active`/`compacted` state with the same stable endpoints. The initial request must emit no cursor on today's unproven schema, and an already issued synthetic/proof-capable cursor must return `cursor_restart_required` with zero messages when any member generation changes. This is the required fail-closed answer to the no-schema-migration constraint.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_conversation_reconciliation_receipts.py -q`

Expected: FAIL because the module is missing.

- [ ] **Step 3: Implement bounded per-lineage storage**

Store under the WebUI state directory, keyed by a hash of profile plus root. Validate current target fingerprint, the declared target-scoped content proof, and all other proof fields; global generation is only a hint that triggers bounded target re-resolution and never substitutes for message-content proof. Writes are temp-file + fsync + replace and never touch session metadata.

Allocate `receipt.generation` from one durable WebUI-owned high-water counter under the receipt store's striped `RLock`: re-read the persisted counter while locked, allocate exactly `max(persisted_high_water, candidate_generation, 0) + 1`, fsync/replace the high-water file, then publish the target receipt. Counter corruption/missing-after-initialization fails closed; it never resets silently. A failed receipt publication may consume an epoch but may not reuse it. Concurrent publishers must receive distinct increasing epochs even when they began from stale receipt objects.

Cursor claims bind both the receipt epoch and a canonical digest of the complete ordered `(member_id, message_generation)` vector plus lineage fingerprint. Every initial/continuation validation recomputes and compares both. Add concurrent publisher, failed replace, deletion/recreation, and ABA tests (`proof A -> B -> A-shaped content` still has higher member generations): no old cursor may validate across any publication/content transition.

- [ ] **Step 4: Add exact fallback publication tests**

Given an exact legacy merge and current state/sidecar watermarks, publish the receipt last. A concurrent sidecar/stat/content-proof change must abort publication. On a proof-capable schema, the next validation succeeds after bounded metadata/proof reads without parsing the sidecar. On an unproven schema, publication may retain shadow diagnostics but must not make cursor mode eligible.

- [ ] **Step 5: Run GREEN**

Run: `./scripts/test.sh tests/test_conversation_reconciliation_receipts.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

Commit: `feat: persist conversation reconciliation receipts`

### Task 3: Receipt validation, legacy fallback, and shadow oracle

**Files:**
- Create: `api/bounded_session_view.py`
- Modify: `api/routes.py:12820-13232`
- Modify: `tests/test_conversation_reconciliation_receipts.py`
- Create: `tests/test_bounded_initial_view.py`

- [ ] **Step 1: Write failing source-selection tests**

Assert initial requests with missing/mismatched receipts select exact legacy mode before reading a cursor page, return no cursor, preserve exact `message_count`, and may publish only after exact comparison. Later cursor requests with any mismatch return 409 `cursor_restart_required` with zero messages.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_bounded_initial_view.py -k "receipt or legacy or restart" -q`

Expected: FAIL because the assembler is missing.

- [ ] **Step 3: Implement proof-first source selection**

Expose a typed assembly context. Validate Stage 1 resolution, Stage 2A capability, receipt, exact count, sidecar/truncation/state watermark, target-scoped content proof, and target fingerprint before any cursor response. Keep the exact legacy merge helper unchanged as oracle/fallback. Perform proof validation both before an initial page and before every continuation page; `unverifiable_current_state` is a named legacy/restart reason.

- [ ] **Step 4: Add shadow-diff tests**

Cover append-only tail, restamped duplicate, interior edit, truncation/clear, compression lineage, sidecar-only legacy, tool pairing, and explicit empty history. Any visible/count/order difference records a reason and returns legacy.

- [ ] **Step 5: Run GREEN and oracle regressions**

Run: `./scripts/test.sh tests/test_bounded_initial_view.py tests/test_webui_state_db_reconciliation.py tests/test_session_lineage_full_transcript.py tests/test_core_data_loss_cases.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

Commit: `feat: gate bounded views on reconciliation proof`

### Task 4: Crash-safe todo view-state projection

**Files:**
- Create: `api/conversation_view_state.py`
- Modify: `api/todo_state.py:67-320`
- Modify: `api/streaming.py:9680-9745`
- Modify: `api/routes.py:15190-15380, 17125-17180`
- Modify: `api/session_ops.py:242-365`
- Create: `tests/test_todo_view_state_projection.py`

- [ ] **Step 1: Write failing projection/CAS tests**

```python
saved = store.compare_and_swap(
    profile="default",
    root_id="root",
    watermark=MessageWatermark(timestamp=20.0, message_id=9),
    target_content_proof_digest="sha256:proof-v1",
    snapshot={"todos": [], "ts": 20.0},
)
assert saved
assert store.read(profile="default", root_id="root").snapshot["todos"] == []
```

Test older replay rejection, explicit empty tombstone, corrupt/version mismatch, profile/root isolation, atomic failure, and no session `updated_at`/unread/title/archive/pin writes. Change an interior durable todo result without changing `(timestamp, message_id)` while advancing its member generation: the old projection must fail validation and cannot be attached to a new receipt.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_todo_view_state_projection.py -q`

Expected: FAIL because the projection store is absent.

- [ ] **Step 3: Implement settled projection and exact rebuild**

Persist normalized snapshots only from a durable message watermark **and** the canonical digest of the ordered target member-generation proof. Projection CAS compares `(target_content_proof_digest, timestamp, message_id)`; read-back is eligible only for the exact current proof. Missing/corrupt/proof-mismatched projection triggers named `legacy_todo_rebuild`: derive from the exact requested-conversation merge, atomically write, return the panel in the same response, and emit no cursor.

- [ ] **Step 4: Add provisional ownership tests**

Represent live todo state in memory/journal only, keyed by active `run_id` plus sequence. Require active owner proof and reject older replay. Do not persist directly from `emit_todo_state`.

- [ ] **Step 5: Define one publication transaction and every acceptance/invalidation boundary**

Add one `publish_settled_conversation_state(...)` integration helper with this strict order:

1. canonical state-db settlement and sidecar save are already durable;
2. re-resolve and exact-merge only the requested lineage, then re-read sidecar generation/stat/truncation and target content proof;
3. derive and CAS the todo projection at the accepted stable message watermark plus target-content-proof digest, including explicit empty tombstones;
4. read the projection back, require its normalized snapshot digest to equal the todo state just derived from the exact merge, then revalidate every proof field and publish the reconciliation receipt **last**, referencing the todo projection generation/watermark/content-proof/snapshot digest.

Wire that helper only at durable acceptance points: after the non-ephemeral terminal `s.save()` plus `completed` turn-journal event in `api/streaming.py`; after `_lazy_import_legacy_webui_session()` has finished all `append_message` calls and `sync_session_usage`; and after the exact legacy branch in `assemble_bounded_session_view` has computed the complete oracle result. The terminal hook is best-effort/queued outside the Agent session lock and aborts rather than publishing if state-db settlement lags.

Every other mutation is an invalidation boundary, not a publication shortcut: `/api/session/clear`, `/api/session/truncate`, `retry_last`, `undo_last`, edit/fork/compression saves, checkpoint saves, `recover_session`, and missing-sidecar materialization all advance `sidecar_generation` (and truncation watermark where applicable), so an old receipt/projection cannot validate. Session deletion removes its receipt/projection/evidence metadata. `emit_todo_state` remains provisional and never calls the settled publisher.

Add crash-point tests after step 1, after todo CAS, before receipt replace, during receipt replace, and after receipt publication for each of terminal-stream, lazy-import, exact-fallback, truncate/clear, and recovery shapes. Only the fully published final state can enter cursor mode; every partial state takes `legacy_todo_rebuild`/legacy merge without resurrecting an older non-empty todo snapshot.

- [ ] **Step 6: Run GREEN and todo regressions**

Run: `./scripts/test.sh tests/test_todo_view_state_projection.py tests/test_todo_state.py tests/test_todo_state_emission.py tests/test_session_todo_state_route.py tests/test_streaming_todo_state_static.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

Commit: `feat: persist crash safe todo view state`

### Task 5: Bounded runtime/recovery overlay

**Files:**
- Create: `api/bounded_runtime_overlay.py`
- Modify: `api/run_journal.py:209-243, 321-340`
- Create: `tests/test_bounded_runtime_overlay.py`

- [ ] **Step 1: Write failing ownership and budget tests**

Cover proven active in-memory owner, matching one-run journal, mismatched session/run/profile, malformed record, row/byte exhaustion, duplicate settled identities, pending user turn, and no active owner.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_bounded_runtime_overlay.py -q`

Expected: FAIL because the bounded overlay is absent.

- [ ] **Step 3: Implement one-run bounded journal read**

Use the existing bounded raw iterator for exactly the owning run file. Do not scan all run files, run stale-sidecar repair, or full-load a settled sidecar. Return typed degradation on malformed/limit/ownership failures.

- [ ] **Step 4: Implement stable-identity overlay merge**

Deduplicate against the settled page, preserve visible chronological order, attach only presentation/runtime fields, and never change canonical lineage/count/title/archive/pin/order metadata.

- [ ] **Step 5: Run GREEN and runtime/redaction gates**

Run: `./scripts/test.sh tests/test_bounded_runtime_overlay.py tests/test_run_journal.py tests/test_run_journal_routes.py tests/test_session_runtime_ownership_invariants.py tests/test_security_redaction.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

Commit: `feat: add bounded active runtime overlay`

### Task 6: Durable shadow evidence and fail-closed enablement

**Files:**
- Create: `api/conversation_shadow_evidence.py`
- Create: `tests/test_conversation_shadow_evidence.py`
- Modify: `api/bounded_session_view.py`
- Modify: `api/routes.py:12820-13322`

- [ ] **Step 1: Write failing aggregate-evidence tests**

Use an injected monotonic wall clock to record 1,000 complete sampled comparisons spanning at least 604,800 seconds. Require `ready=True` only for one exact implementation/schema/profile cohort with `sample_count >= 1000`, `last_sample_at - first_sample_at >= 7 days`, zero visible identity/order/count/truncation/tool-pair differences, and all current query/receipt/view-state gates passing. Missing, corrupt, future-version, clock-regressed, mixed-cohort, or incomplete evidence is not ready.

- [ ] **Step 2: Test latched disablement**

One semantic difference atomically records its typed reason, increments `difference_count`, sets `disabled_at`, and latches the cohort disabled. Later matches and environment flags cannot clear it. Only a new checked-in implementation evidence version starts a new cohort; old evidence remains auditable. Store counts/timestamps/reason codes and build/schema IDs only, never transcript content, paths, or credentials.

- [ ] **Step 3: Implement durable sampled evidence**

Write one small atomic/fsynced aggregate under the WebUI state directory. Record a sample only after the candidate and unchanged exact legacy oracle both complete for the same resolved lineage and request generation. Sampling must be deterministic/bounded and must not change the response. Expose a typed `ShadowReadiness` used by server cursor enablement and later browser bootstrap; a manual `on` flag can request evaluation but cannot bypass readiness.

- [ ] **Step 4: Run GREEN**

Run: `./scripts/test.sh tests/test_conversation_shadow_evidence.py tests/test_bounded_initial_view.py -k "shadow or evidence or readiness or difference" -q`

Expected: PASS.

- [ ] **Step 5: Detect changes and commit**

Commit: `feat: persist bounded view shadow evidence`

### Task 7: Complete negotiated initial-view assembly

**Files:**
- Modify: `api/bounded_session_view.py`
- Modify: `api/routes.py:12820-13232`
- Modify: `tests/test_bounded_initial_view.py`
- Modify: `scripts/benchmark_conversation_load.py`
- Modify: `tests/test_conversation_load_benchmark.py`
- Modify: `ARCHITECTURE.md`
- Modify: `TESTING.md`

- [ ] **Step 1: Write failing assembly-order tests**

Require: resolve -> validate all proof -> choose one source -> page/legacy read -> runtime overlay -> settled/provisional todo -> one redaction -> response. Verify exact cursor-mode `message_count` from receipt plus proven live delta.

Assert the complete successful cursor wire contract: `message_page` has exactly the required stable fields `mode`, `before_cursor`, `has_more`, `visible_count`, `raw_rows_examined`, and `serialized_bytes`; cursor mode exposes neither `_messages_offset` nor `_messages_truncated`. Assert explicit legacy mode retains numeric coordinates and never exposes a cursor. Run these through the real route, not only assembler unit tests.

- [ ] **Step 2: Add independent gates**

Implement defaults:

```text
HERMES_WEBUI_RECEIPT_FAST_PATH=0
HERMES_WEBUI_DERIVED_VIEW_STATE_READS=0
HERMES_WEBUI_BOUNDED_VIEW_SHADOW=1
```

Receipt/projection writes may run while reads remain off. Disabling either read gate restores exact one-conversation legacy behavior.

Even when all environment gates request `on`, public cursor mode remains unavailable unless current `ShadowReadiness.ready` is true and the target-scoped Agent content-proof capability is present. Return the exact legacy response with a typed fallback reason otherwise.

- [ ] **Step 3: Run route GREEN tests**

Run: `./scripts/test.sh tests/test_bounded_initial_view.py tests/test_session_cursor_paging_route.py tests/test_session_tail_payload.py tests/test_security_redaction.py tests/test_session_lineage_full_transcript.py -q`

Expected: PASS.

- [ ] **Step 4: Extend and run the complete-view mechanical/SLO benchmark**

Extend the shared runner's `initial-view` stage. Assert exactly one canonical resolution; capability/resolver/paging SQL ceilings; bounded raw rows and independent 2 MiB/512 KiB byte budgets; all five named diagnostic stages; exact receipt count plus bounded live delta; no full scan; warm 30-row p95 `<1s`; process-cold p95 `<2s`; no sample `>5s`; and base/scaling SQL/raw-row equality with latency regression at most `max(100ms,20%)`. Concurrency-4 for 20 rounds permits no request over 5 seconds and no cursor/legacy mode drift within a load.

Run:

```bash
./scripts/test.sh tests/test_conversation_load_benchmark.py -k "initial_view or diagnostics or scaling or concurrency" -q
.venv/bin/python scripts/benchmark_conversation_load.py --stage initial-view --fixture .verify/conversation-load/current-base --expect-mode legacy --warm 40 --process-cold 20 --output .verify/conversation-load/stage-2b-current.json
.venv/bin/python scripts/benchmark_conversation_load.py --stage initial-view --fixture .verify/conversation-load/proof-base --expect-mode cursor_v1 --synthetic-ready-evidence --visible-limit 30 --warm 40 --process-cold 20 --concurrency 4 --stress-rounds 20 --compare-fixture .verify/conversation-load/proof-scaling --output .verify/conversation-load/stage-2b-proof.json
```

The `current` cohort command must assert `unverifiable_current_state`, exact legacy output, and no cursor. Only the manifest-declared synthetic `proof-v1` cohort may accept `--synthetic-ready-evidence`; the runner rejects that flag for any current/production manifest. Bounded cursor SLO assertions apply only to `proof-v1`, while current-schema fail-closed correctness is mandatory.

- [ ] **Step 5: Run Stage 2B regression bundle**

Run:

```bash
./scripts/test.sh \
  tests/test_session_sidecar_generation.py \
  tests/test_conversation_reconciliation_receipts.py \
  tests/test_conversation_shadow_evidence.py \
  tests/test_todo_view_state_projection.py \
  tests/test_bounded_runtime_overlay.py \
  tests/test_bounded_initial_view.py \
  tests/test_webui_state_db_reconciliation.py \
  tests/test_session_lineage_full_transcript.py \
  tests/test_session_runtime_ownership_invariants.py \
  tests/test_security_redaction.py -q
```

Expected: PASS.

- [ ] **Step 6: Run ruff/diff/GitNexus gates**

Run: `.venv/bin/ruff check api/models.py api/session_recovery.py api/conversation_receipts.py api/conversation_view_state.py api/conversation_shadow_evidence.py api/bounded_runtime_overlay.py api/bounded_session_view.py api/routes.py api/streaming.py api/todo_state.py scripts/benchmark_conversation_load.py`

Run: `git diff --check`

Expected: exit 0.

- [ ] **Step 7: Commit docs/integration**

Commit: `feat: assemble proof bounded conversation views`
