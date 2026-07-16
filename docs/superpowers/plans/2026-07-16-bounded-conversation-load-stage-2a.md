# Bounded Conversation Load Stage 2A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dormant, negotiated cursor backend whose database work is physically bounded and whose legacy behavior remains unchanged.

**Architecture:** A new `api/session_message_paging.py` owns read-only schema capability checks, bounded indexed pages, and integrity-protected opaque cursors. `/api/session` gains a localized off/shadow/on negotiation branch, but public cursor success remains unavailable until Stage 2B supplies a validated reconciliation receipt and exact merged `message_count`.

**Tech Stack:** Python, SQLite, HMAC-SHA256, canonical JSON/base64url, existing route server, pytest via `./scripts/test.sh`.

---

## File structure

- Create `api/session_message_paging.py`: capability, cursor claims/codec, bounded page reader.
- Modify `api/routes.py`: negotiation parsing, default-off/shadow integration only.
- Create cursor capability/reader/codec/route/shadow test modules.
- Modify `ARCHITECTURE.md` and `TESTING.md`: gate and query-budget contract.

## Safety boundary

Do not modify `get_state_db_session_messages`, `merge_session_messages_append_only`, compatibility detail wire shape, or existing numeric helpers. GitNexus reports the generic message reader HIGH risk; `handle_get` is manually HIGH risk. All new server behavior is additive and defaults off. A cursor request never writes Agent tables/indexes.

### Task 1: Read-only paging capability gate

**Files:**
- Create: `api/session_message_paging.py`
- Create: `tests/test_state_db_message_cursor_capability.py`

- [ ] **Step 1: Write failing supported-schema tests**

Define the desired result:

```python
cap = inspect_message_paging_capability(conn, db_identity=(str(db), stat_key))
assert cap.supported is True
assert cap.schema_version == 7
assert cap.ordering_columns == ("timestamp", "id")
assert cap.message_index == "idx_messages_session"
```

Add missing table/ID/index/session-parent-index, TEXT/non-normalizable timestamp, active-column variants, and read-only assertions. Snapshot `PRAGMA index_list` before/after to prove no mutation.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_state_db_message_cursor_capability.py -q`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Add cache/query-ceiling tests**

Assert at most six SQL statements on a cache miss, zero repeated schema probes on a hit, and invalidation when database identity or `PRAGMA schema_version` changes.

- [ ] **Step 4: Implement frozen capability data and inspection**

```python
@dataclass(frozen=True)
class MessagePagingCapability:
    supported: bool
    schema_version: int
    message_index: str | None
    has_active: bool
    fallback_reason: str | None
```

Require stable message `id`, `session_id`, role/content, normalizable timestamp, direct session lookup, and an index led by session/timestamp (optionally active). Cache by database stat identity and schema version. Never self-heal.

- [ ] **Step 5: Run GREEN and schema regressions**

Run: `./scripts/test.sh tests/test_state_db_message_cursor_capability.py tests/test_issue3762_importable_rows_schema_guard.py tests/test_issue3887_index_prime.py -q`

Expected: PASS; existing collection index self-heal tests remain unchanged while cursor inspection never writes.

- [ ] **Step 6: Detect changes and commit**

Commit: `feat: gate cursor paging on indexed state schema`

### Task 2: Physically bounded state message pages

**Files:**
- Modify: `api/session_message_paging.py`
- Create: `tests/test_state_db_message_cursor_reader.py`

- [ ] **Step 1: Write failing page-order tests**

Specify an API receiving Stage 1 resolution and explicit DB path:

```python
page = read_state_db_message_page(
    db_path=db,
    resolution=resolution,
    visible_limit=30,
    cursor=None,
)
assert page.mode == "cursor_v1"
assert [m["content"] for m in page.messages] == expected_chronological_tail
assert page.raw_rows_examined <= 256
```

Cover cross-member timestamp ties, NULL timestamps, inactive rows, exact `_state_db_message_id`, unrelated rows, and short pages caused by hidden rows.

At both the route boundary and `read_state_db_message_page` boundary, assert `visible_limit` is an integer in the inclusive range 1..100. Negotiated values below 1, above 100, booleans, floats, repeated/conflicting values, and non-numeric strings return 400 before opening the database. Direct internal calls raise the typed validation error. Legacy requests retain their existing parsing semantics.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_state_db_message_cursor_reader.py -q`

Expected: FAIL because the reader/result do not exist.

- [ ] **Step 3: Write the range, budget, and query-plan tests**

For every accepted boundary `N` including 1 and 100, assert raw rows never exceed `max(256, min(2048, 8*N))`, SQL statements do not exceed `3 + D`, no `fetchall()`, and `EXPLAIN QUERY PLAN` uses the per-session message index. Add 10,000 unrelated sessions and one million unrelated message rows while preserving counts. Repeat the 1..100 validation inside the reader so a route regression cannot bypass the physical-work contract.

- [ ] **Step 4: Implement bounded per-member quotas and k-way merge**

Use one ordered, limited query per lineage member, allocating the global raw budget across members including sentinels. Merge newest-first in Python, filter renderability/inactive rows within the accounted raw budget, then reverse selected rows for chronological wire order. The continuation boundary is the last raw row examined per member, never a visible offset.

- [ ] **Step 5: Run GREEN**

Run: `./scripts/test.sh tests/test_state_db_message_cursor_reader.py tests/test_state_db_active_filter.py tests/test_state_db_readonly_reads_models.py tests/test_issue1494_state_db_fd_leak.py -q`

Expected: PASS.

- [ ] **Step 6: Detect changes and commit**

Commit: `feat: add bounded state message page reader`

### Task 3: Tool-pair and serialized-byte closure

**Files:**
- Modify: `api/session_message_paging.py`
- Modify: `tests/test_state_db_message_cursor_reader.py`

- [ ] **Step 1: Write failing closure tests**

Cover a tool call/result crossing the base boundary, a partner within 64 extra raw rows/512 KiB, a partner outside that allowance, a single oversized tool payload, multimodal content, and redaction-expanded/prepared bytes.

```python
assert page.tool_pair_status == "complete"
assert page.raw_rows_examined <= base_budget + 64
assert page.serialized_bytes <= 2_621_440
```

Assert the budgets independently, not only as a combined 2.5 MiB ceiling: ordinary prepared/redacted page bytes must stop at 2 MiB, only required tool-pair closure may consume the separate 512 KiB allowance, and closure may inspect at most 64 additional raw rows. Test one byte below, exactly at, and one byte above each limit. A single oversized message must use the existing bounded representation and must not borrow the closure budget unless it is completing a required pair.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_state_db_message_cursor_reader.py -k "tool or byte or payload" -q`

Expected: FAIL on unimplemented closure semantics.

- [ ] **Step 3: Implement bounded preparation**

Reuse or extract pure renderability/payload-shaping predicates without changing legacy callers. Spend at most the normative closure allowance. On an initial page outside the allowance, return a typed `legacy_required` outcome with no cursor; on a later cursor page, return typed `cursor_restart_required` with no messages.

- [ ] **Step 4: Run GREEN and legacy payload tests**

Run: `./scripts/test.sh tests/test_state_db_message_cursor_reader.py tests/test_session_tail_payload.py tests/test_tool_call_history_paging.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Commit: `feat: enforce cursor page payload bounds`

### Task 4: Integrity-protected opaque cursor

**Files:**
- Modify: `api/session_message_paging.py`
- Create: `tests/test_session_message_cursor.py`

- [ ] **Step 1: Write failing codec tests**

Test deterministic canonical payload encoding, round trip, tampering, oversized token before decode, wrong version, wrong profile/database/source mode/fingerprint/receipt generation, and process-key rotation.

```python
token = encode_message_cursor(claims, signing_key=b"k" * 32)
decoded = decode_message_cursor(token, signing_key=b"k" * 32, expected=expected)
assert decoded == claims
```

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_session_message_cursor.py -q`

Expected: FAIL because codec APIs are missing.

- [ ] **Step 3: Implement the cursor contract**

Use compact canonical JSON plus base64url and HMAC-SHA256 with constant-time verification. The default server signing key is process-ephemeral, so restart safely invalidates cursors. Claims include version, profile, canonical ID, target-lineage fingerprint, source mode, DB identity, global generation hint, an optional `receipt_generation` that remains `None` while Stage 2A is dormant, and per-member raw boundaries. No transcript content or paths appear in the token.

- [ ] **Step 4: Run GREEN**

Run: `./scripts/test.sh tests/test_session_message_cursor.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Commit: `feat: protect message paging cursors`

### Task 5: Default-off route negotiation

**Files:**
- Modify: `api/routes.py:12820-13232`
- Create: `tests/test_session_cursor_paging_route.py`

- [ ] **Step 1: Write failing compatibility matrix tests**

Assert:

- no `message_paging` produces the exact legacy response and keys;
- `cursor_v1` with gate off/degraded returns legacy coordinates plus `message_page.mode="legacy"`;
- `msg_cursor` and `msg_before` together return 400;
- malformed/cross-profile cursors return 400 with no messages;
- stale bound state returns 409 `cursor_restart_required` with no messages;
- browser/numeric callers remain unchanged.
- negotiated `msg_limit` is validated as 1..100 before database access; invalid values return 400 and never clamp into a larger physical read.

With an injected proof-capable Stage 2B provider, lock the successful cursor wire shape exactly: top-level response keeps compatibility metadata and messages, `message_page` contains `mode="cursor_v1"`, `before_cursor` (string or null), boolean `has_more`, integer `visible_count`, integer `raw_rows_examined`, and integer `serialized_bytes`; top-level `_messages_offset` and `_messages_truncated` are absent. No extra cursor coordinate is inferred from `message_count`. In legacy mode, preserve the current `_messages_offset`/`_messages_truncated` contract and require `message_page.mode="legacy"` with no opaque cursor.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_session_cursor_paging_route.py -q`

Expected: FAIL because negotiation is absent.

- [ ] **Step 3: Add localized mode parsing and gate**

Implement `HERMES_WEBUI_MESSAGE_CURSOR_V1=off|shadow|on`, default `off`. Reuse the Stage 1 resolution object. In Stage 2A, even `on` must return legacy unless a Stage 2B exact-count/receipt provider is present and validates; never run `COUNT(*)` to fake readiness.

- [ ] **Step 4: Run GREEN plus numeric regressions**

Run: `./scripts/test.sh tests/test_session_cursor_paging_route.py tests/test_parallel_session_switch.py tests/test_session_tail_payload.py tests/test_shared_state_db_session_projection.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

Commit: `feat: negotiate cursor paging on session detail`

### Task 6: Shadow oracle and Stage 2A acceptance

**Files:**
- Create: `tests/test_session_cursor_paging_shadow.py`
- Modify: `scripts/benchmark_conversation_load.py`
- Modify: `tests/test_conversation_load_benchmark.py`
- Modify: `ARCHITECTURE.md`
- Modify: `TESTING.md`

- [ ] **Step 1: Write page-concatenation oracle tests**

Across compression lineage, duplicate/restamped/edited rows, inactive history, missing timestamps, tools, and schema degradation, concatenate bounded pages and compare exact visible identity/order with the unchanged legacy merge oracle.

- [ ] **Step 2: Implement shadow diagnostics only**

In `shadow`, execute the bounded reader for sampled eligible test requests, compare without changing the response, and record stage/query/row/byte/fallback reason without message content.

- [ ] **Step 3: Run the Stage 2A gate**

Run:

```bash
./scripts/test.sh \
  tests/test_state_db_message_cursor_capability.py \
  tests/test_state_db_message_cursor_reader.py \
  tests/test_session_message_cursor.py \
  tests/test_session_cursor_paging_route.py \
  tests/test_session_cursor_paging_shadow.py \
  tests/test_state_db_active_filter.py \
  tests/test_session_tail_payload.py \
  tests/test_parallel_session_switch.py \
  tests/test_session_lineage_full_transcript.py -q
```

Expected: PASS; public cursor success remains dormant without Stage 2B proof.

- [ ] **Step 4: Extend and run the mechanical message-page benchmark gate**

Extend the checked-in Stage 1 runner, not a new script. Its `message-page` stage must assert capability inspection `<=6` SQL on a cache miss and zero repeated schema probes on a hit, paging SQL `<=3+D`, raw rows `<=max(256,min(2048,8N))+64`, ordinary bytes `<=2 MiB`, closure bytes `<=512 KiB`, combined bytes `<=2.5 MiB`, one canonical resolution, and no unscoped full scan. It must record all five diagnostic stages even when dormant stages are zero.

Run:

```bash
./scripts/test.sh tests/test_conversation_load_benchmark.py -k "message_page or scaling or concurrency" -q
.venv/bin/python scripts/benchmark_conversation_load.py --stage message-page --fixture .verify/conversation-load/proof-base --visible-limit 30 --warm 40 --process-cold 20 --concurrency 4 --stress-rounds 20 --compare-fixture .verify/conversation-load/proof-scaling --output .verify/conversation-load/stage-2a.json
```

The scaling profile must keep exact SQL/raw-row counts; warm and process-cold p95 regression is at most `max(100ms,20%)`; no stress request exceeds 5 seconds or drifts between cursor and legacy source mode.

- [ ] **Step 5: Run ruff, diff check, and GitNexus detection**

Run: `.venv/bin/ruff check api/session_message_paging.py api/routes.py tests/test_state_db_message_cursor_capability.py tests/test_state_db_message_cursor_reader.py tests/test_session_message_cursor.py tests/test_session_cursor_paging_route.py tests/test_session_cursor_paging_shadow.py`

Run: `git diff --check`

Expected: exit 0.

- [ ] **Step 6: Commit docs/tests**

Commit: `test: lock bounded cursor paging query shape`
