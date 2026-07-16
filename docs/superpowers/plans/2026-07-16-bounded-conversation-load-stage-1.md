# Bounded Conversation Load Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace full-collection session resolution with one indexed, read-only resolution receipt reused by browser and compatibility detail paths.

**Architecture:** `api/agent_sessions.py` owns a frozen `SharedSessionResolution` and one bounded ancestor/descendant traversal using the existing continuation guard. Route adapters consume its canonical row and ordered root-to-tip `member_ids`; collection APIs and existing generic message-reader defaults remain unchanged.

**Tech Stack:** Python 3.11-3.13, SQLite, dataclasses, existing HTTP server, pytest through `./scripts/test.sh`.

---

## File structure

- Modify `api/agent_sessions.py`: resolution receipt, indexed traversal, compatibility ID wrapper.
- Create `api/session_history.py`: read-only full-history adapter for already-resolved member IDs.
- Modify `api/routes.py`: localized `/api/session` and shared compatibility payload integration.
- Create `tests/test_bounded_shared_session_resolution.py`: semantic and query-shape contract.
- Create `tests/test_resolved_session_history.py`: member-order and no-rewalk contract.
- Create `tests/test_bounded_session_detail_routes.py`: one-resolution and wire-compatibility contract.
- Create `scripts/generate_conversation_load_fixture.py`: deterministic base/scaling fixture materializer into an ignored state directory.
- Create `scripts/benchmark_conversation_load.py`: isolated authenticated benchmark runner and JSON receipt writer.
- Create `tests/test_conversation_load_benchmark.py`: generator determinism, diagnostics, query-shape, and budget assertions.
- Modify `ARCHITECTURE.md`: collection-versus-entity lookup boundary.

## Impact gate

GitNexus CLI reports `read_session_lineage_metadata` MEDIUM risk (13 direct callers). `get_state_db_session_messages` is HIGH risk (10 direct callers and three transitive flow families), so Stage 1 must not change that function. `handle_get` is manually HIGH risk because it owns every GET route; edits stay inside the `/api/session` branch. Before each production edit, rerun upstream impact for the exact symbol and stop for any newly reported HIGH/CRITICAL scope.

### Task 1: Indexed shared-session resolution

**Files:**
- Modify: `api/agent_sessions.py:388-442, 745-778, 1297-1599`
- Create: `tests/test_bounded_shared_session_resolution.py`

- [ ] **Step 1: Write the semantic receipt tests**

Add a fixture with root -> middle -> tip compression rows plus fork, delegate, tool, and cross-source children. Define the wished-for API:

```python
from api.agent_sessions import resolve_shared_session

resolution = resolve_shared_session(db, "root")
assert resolution.status == "found"
assert resolution.requested_id == "root"
assert resolution.canonical_id == "tip"
assert resolution.root_id == "root"
assert resolution.tip_id == "tip"
assert resolution.member_ids == ("root", "middle", "tip")
assert resolution.canonical_row["title"] == "Root title"
assert resolution.lineage_fingerprint
```

Add separate tests for direct non-snapshot stability, `mode="history"`, missing IDs, old schemas, cycles, 256 hops, ambiguous compression siblings, and branch/delegate/tool/cross-source isolation. The chosen path must match `read_shared_session_rows()` on valid fixtures without calling it.

- [ ] **Step 2: Run RED and verify the missing API is the failure**

Run: `./scripts/test.sh tests/test_bounded_shared_session_resolution.py -q`

Expected: FAIL because `resolve_shared_session` and `SharedSessionResolution` do not exist.

- [ ] **Step 3: Add query-shape tests before implementation**

Trace SQLite statements and monkeypatch collection readers to raise. Assert:

```python
assert not any(" FROM messages " in normalized for normalized in statements)
assert not any("SELECT" in q and "FROM sessions" in q and "WHERE" not in q for q in statements)
assert query_count_with_10k_unrelated == query_count_without_unrelated
```

Also assert read-only connection closure and no index/schema mutation.

- [ ] **Step 4: Verify the query-shape tests fail for the current resolver**

Run: `./scripts/test.sh tests/test_bounded_shared_session_resolution.py -q`

Expected: FAIL because the current wrapper calls `read_shared_session_rows(limit=None)`.

- [ ] **Step 5: Implement the frozen receipt and shared indexed traversal**

Add a result contract equivalent to:

```python
@dataclass(frozen=True)
class SharedSessionResolution:
    requested_id: str
    canonical_id: str
    root_id: str
    tip_id: str
    member_ids: tuple[str, ...]
    canonical_row: Mapping[str, Any] | None
    lineage_fingerprint: str
    global_projection_generation_hint: int | None
    mode: Literal["navigation", "history"]
    status: Literal["found", "missing", "degraded", "ambiguous"]
```

Extract the PK/`idx_sessions_parent` row discovery used by `read_session_lineage_metadata()`. Hold one explicit read transaction, reuse `_is_continuation_session(..., compression_only=True)`, cap both directions at 256 hops, select only the deterministic root-to-canonical path for `member_ids`, and compute the fingerprint from ordered member identity plus lineage-selection fields. Visited siblings may detect ambiguity but must not enter `member_ids`.

- [ ] **Step 6: Keep the old public ID API as a wrapper**

```python
def resolve_shared_session_id(db_path: Path, session_id: str) -> str:
    return resolve_shared_session(db_path, session_id).canonical_id
```

For missing/degraded/ambiguous results, `canonical_id` remains the requested safe ID.

- [ ] **Step 7: Run focused GREEN tests**

Run: `./scripts/test.sh tests/test_bounded_shared_session_resolution.py tests/test_pr1370_lineage_metadata_perf_and_orphan.py tests/test_issue5455_lineage_readonly_reads.py tests/test_issue1494_state_db_fd_leak.py -q`

Expected: PASS with unchanged legacy lineage-metadata behavior.

- [ ] **Step 8: Run GitNexus change detection and commit**

Run: `npx gitnexus detect-changes --scope staged`

Expected: only resolver/lineage flows and their tests.

Commit: `perf: add indexed shared session resolution`

### Task 2: Resolution-aware full-history adapter

**Files:**
- Create: `api/session_history.py`
- Create: `tests/test_resolved_session_history.py`

- [ ] **Step 1: Write failing adapter tests**

Specify a focused reader that receives an explicit database path and ordered members:

```python
messages = read_resolved_session_history(
    db_path=db,
    member_ids=("root", "middle", "tip"),
    include_inactive=False,
)
assert [m["content"] for m in messages] == ["root", "middle", "tip"]
```

Assert missing/duplicate member IDs, NULL/tied timestamps, tool metadata, inactive rows, read-only connection closure, and that no `sessions` query or lineage helper runs.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_resolved_session_history.py -q`

Expected: FAIL because `api.session_history` does not exist.

- [ ] **Step 3: Implement the smallest read-only adapter**

Use `open_state_db_readonly`, introspect message columns, preserve the existing optional metadata decoding, query only `WHERE session_id IN (...)`, and return chronological rows ordered by normalized timestamp, stable row ID, and member order where needed. Do not import or call `get_state_db_session_messages` and do not create indexes.

- [ ] **Step 4: Run GREEN plus reader invariants**

Run: `./scripts/test.sh tests/test_resolved_session_history.py tests/test_state_db_active_filter.py tests/test_state_db_readonly_reads_models.py -q`

Expected: PASS.

- [ ] **Step 5: Detect changes and commit**

Commit: `perf: read history from resolved session members`

### Task 3: Compatibility detail payload reuse

**Files:**
- Modify: `api/routes.py:17185-17251`
- Create: `tests/test_bounded_session_detail_routes.py`
- Modify: `tests/test_shared_state_db_session_projection.py:176-184, 769-817`

- [ ] **Step 1: Write failing compatibility tests**

Assert metadata detail resolves once, never calls `read_shared_session_rows`, and does not read messages. Assert message detail passes the receipt members to `read_resolved_session_history`. Preserve every existing field and lazy import behavior.

```python
assert payload["requested_session_id"] == "root"
assert payload["canonical_session_id"] == "tip"
assert payload["lineage"]["root_id"] == "root"
assert history_calls == [("root", "middle", "tip")]
```

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_bounded_session_detail_routes.py tests/test_shared_state_db_session_projection.py -q`

Expected: FAIL because the payload still builds the full shared projection.

- [ ] **Step 3: Refactor `_shared_session_detail_payload`**

Accept an optional `resolution`. When absent, resolve exactly once. Consume `canonical_row` directly. Read messages only when `include_messages=True`. After a successful one-session legacy import, resolve once more; never scan/reconcile unrelated sessions.

- [ ] **Step 4: Verify compatibility GREEN**

Run: `./scripts/test.sh tests/test_bounded_session_detail_routes.py tests/test_shared_state_db_session_projection.py tests/test_session_lineage_full_transcript.py -q`

Expected: PASS with unchanged compatibility response shapes.

- [ ] **Step 5: Detect changes and commit**

Commit: `perf: reuse resolution in shared session detail`

### Task 4: Browser detail route integration and diagnostics

**Files:**
- Modify: `api/routes.py:12820-13322`
- Modify: `tests/test_bounded_session_detail_routes.py`
- Modify: `tests/test_issue1855_request_diagnostics.py`

- [ ] **Step 1: Write failing one-resolution route tests**

Cover `messages=0` and current numeric message loads. Count resolver calls, force collection readers to raise, and assert `state.db` title/workspace/archive/pin beat stale sidecar fields. Add profile mismatch and missing-ID non-disclosure cases.

- [ ] **Step 2: Write the failing diagnostic assertion**

Require a `canonical_resolution` stage emitted before `get_session` and preserved on early returns.

- [ ] **Step 3: Run RED**

Run: `./scripts/test.sh tests/test_bounded_session_detail_routes.py tests/test_issue1855_request_diagnostics.py -q`

Expected: FAIL on duplicate/unbounded resolution or missing diagnostic stage.

- [ ] **Step 4: Localize the route change**

Resolve once at the start of the `/api/session` branch. Use `resolution.canonical_id`, `canonical_row`, and `member_ids`; do not weaken load-generation, profile, redaction, runtime, todo, numeric pagination, or legacy fallback behavior. Pass the resolution object into any compatibility builder instead of reconstructing lineage.

- [ ] **Step 5: Run focused GREEN and reconciliation gates**

Run: `./scripts/test.sh tests/test_bounded_session_detail_routes.py tests/test_issue1855_request_diagnostics.py tests/test_session_tail_payload.py tests/test_webui_state_db_reconciliation.py tests/test_session_lineage_full_transcript.py tests/test_session_active_profile_authorization.py -q`

Expected: PASS.

- [ ] **Step 6: Detect changes and commit**

Commit: `perf: reuse canonical resolution in session loads`

### Task 5: Architecture and Stage 1 acceptance

**Files:**
- Modify: `ARCHITECTURE.md:308-318`
- Create: `scripts/generate_conversation_load_fixture.py`
- Create: `scripts/benchmark_conversation_load.py`
- Create: `tests/test_conversation_load_benchmark.py`

- [ ] **Step 1: Document the entity/collection boundary**

State that entity loads are indexed and receipt-based, collection projections remain list/export-only, Stage 1 performs no state mutation, and missing/old schemas fail closed to legacy requested-ID behavior.

- [ ] **Step 2: Run the Stage 1 regression bundle**

Run:

```bash
./scripts/test.sh \
  tests/test_bounded_shared_session_resolution.py \
  tests/test_resolved_session_history.py \
  tests/test_bounded_session_detail_routes.py \
  tests/test_shared_state_db_session_projection.py \
  tests/test_pr1370_lineage_metadata_perf_and_orphan.py \
  tests/test_session_lineage_full_transcript.py \
  tests/test_webui_state_db_reconciliation.py \
  tests/test_issue1855_request_diagnostics.py -q
```

Expected: PASS.

- [ ] **Step 3: Check in the deterministic benchmark fixture generator**

Implement the exact design fixture as generated data, never as a committed multi-gigabyte artifact: 2,560 sessions with 2,000 archived rows, one 12-segment/20,000-message target lineage, inactive/hidden/multimodal/missing-timestamp/tool-pair rows, 100 MiB valid- and mismatched-receipt sidecars, plus a scaling profile with 10,000 unrelated sessions and 1,000,000 unrelated messages. Accept an explicit output directory and seed; reject a real Hermes home/state path. Emit a manifest containing fixture schema version, seed, row counts, target IDs, expected visible identity digest, Agent-contract cohort, and file hashes. A scaled-down test profile must exercise the identical generator code in CI.

Materialize two explicit schema cohorts at each scale from the same logical data:

- `current`: today's unproven Agent schema, with no target message-generation capability. It must always fail closed to legacy for public cursor/receipt paths.
- `proof-v1`: a synthetic, explicitly versioned future Agent contract with a capability marker plus Agent-owned test triggers that monotonically advance each affected `sessions.message_generation` for every message insert/update/delete/session move and `active`/`compacted` change. It exists only to exercise bounded cursor mechanics/SLOs and must never be mistaken for current production support.

- [ ] **Step 4: Add the shared isolated benchmark runner and diagnostics contract**

The runner starts an isolated WebUI process using explicit `HERMES_HOME`, `HERMES_WEBUI_STATE_DIR`, and port, authenticates through the existing test helper, primes once, and emits machine-readable samples plus CPU, memory, OS, Python, SQLite, DB size, and commit. It records these named stages from before resolver entry: `canonical_resolution`, `state_message_page`, `runtime_overlay`, `derived_view_state`, and `redaction_and_serialize`. Each sample records SQL count, lineage depth, requested/returned rows, raw rows examined, serialized bytes, source mode, receipt generation, cache result, fallback reason, and duplicate resolver count without content or paths.

Add subcommands/stages so later plans extend the same runner rather than inventing ad-hoc timing scripts. The Stage 1 `resolution` gate asserts capability plus resolver SQL `<= 10 + 2D` cold and `<= 4 + 2D` warm, no unscoped `sessions`/`messages` scan in captured `EXPLAIN QUERY PLAN`, one resolver call per request, metadata-detail p95 `<250ms`, and no request `>5s`. p95 is nearest-rank.

Run the deterministic unit/mini-fixture gate:

```bash
./scripts/test.sh tests/test_conversation_load_benchmark.py -k "fixture or resolution or diagnostics" -q
```

Run the representative local-SSD gate (artifacts stay ignored):

```bash
.venv/bin/python scripts/generate_conversation_load_fixture.py --scale base --agent-contract current --output .verify/conversation-load/current-base
.venv/bin/python scripts/generate_conversation_load_fixture.py --scale scaling --agent-contract current --output .verify/conversation-load/current-scaling
.venv/bin/python scripts/generate_conversation_load_fixture.py --scale base --agent-contract proof-v1 --output .verify/conversation-load/proof-base
.venv/bin/python scripts/generate_conversation_load_fixture.py --scale scaling --agent-contract proof-v1 --output .verify/conversation-load/proof-scaling
.venv/bin/python scripts/benchmark_conversation_load.py --stage resolution --fixture .verify/conversation-load/current-base --warm 40 --process-cold 20 --concurrency 4 --stress-rounds 20 --compare-fixture .verify/conversation-load/current-scaling --output .verify/conversation-load/stage-1.json
```

The scaling run must preserve SQL/row counts and may regress warm/process-cold p95 by no more than `max(100ms, 20%)`; concurrency 4 permits no request over 5 seconds.

- [ ] **Step 5: Run formatting and diff checks**

Run: `.venv/bin/ruff check api/agent_sessions.py api/session_history.py api/routes.py scripts/generate_conversation_load_fixture.py scripts/benchmark_conversation_load.py tests/test_bounded_shared_session_resolution.py tests/test_resolved_session_history.py tests/test_bounded_session_detail_routes.py tests/test_conversation_load_benchmark.py`

Run: `git diff --check`

Expected: both exit 0.

- [ ] **Step 6: Run GitNexus staged scope and commit docs**

Commit: `docs: document bounded session resolution`

- [ ] **Step 7: Run the full supported test harness**

Run: `./scripts/test.sh`

Expected: all tests pass, except the already recorded order-dependent baseline Agent import anomaly if it reproduces unchanged; rerun that exact test in isolation and record both receipts.
