# Context Budget Guard Design

## Goal

Prevent Hermes from sending oversized or duplicated provider requests while
preserving the active task across compaction.

## Baseline and isolation

- WebUI base: `9841337a56aed4aea88ff00b1a856e425450ccf5`
- Agent base: `a69e9780141ebea8aa2a1666eecf1b8dba9c54f9`
- Work occurs only on the dedicated `codex/context-budget-guard-*` branches.
- Existing checkouts, worktrees, running services, and user state are untouched.
- Development and review use inherited Luna threads. No live Zeus dependency is
  required for implementation or automated verification.

## Design

### One provider-boundary guard

Hermes Agent owns one admission check immediately before every provider call,
including tool-loop calls, retries, and model fallbacks. It receives the final
request after prompt, middleware, and tool assembly.

The guard performs one bounded sequence:

1. Measure the final request against the selected model's real input budget:
   `context window - output reserve - safety margin`.
2. Remove duplicate historical rows and replace old tool bodies and old
   reasoning with existing compact receipts.
3. If the request still does not fit, run the existing compressor in place.
4. Rebuild and measure the request again.
5. Dispatch only when it fits; otherwise fail closed before network I/O.

The guard reuses the current estimator, deterministic tool pruning, compressor,
and in-place persistence. It is not a plugin, service, retrieval layer, or new
context framework.

### In-place continuity

Compaction keeps the same logical and physical session ID. The full visible
transcript remains archived in state storage, while model context becomes the
latest verified compression summary/checkpoint plus a bounded recent tail.

The legacy rotation/whole-database-adoption path is not used by automatic
compression. WebUI must never create an empty recovery conversation. If a
request remains irreducible after compaction, WebUI reports that specific
failure on the existing session and preserves the last valid bounded context.

### Failure behavior

- Provider fallback resolves a fresh context budget and reruns admission.
- A provider context-limit rejection may compact and retry once.
- Tool calls completed before the retry are represented by receipts and are not
  executed again.
- Compression or summary failure preserves the previous valid model context;
  it never adopts a partial snapshot and never creates blank context.
- Admission telemetry records category sizes and decisions, not prompt content.

## Non-goals

- LCM, RAG, a vector database, or another context engine.
- A new model tool or additional provider schema.
- Redesigning Tool Search or the system prompt in this change.
- Editing live Hermes configuration, sessions, credentials, or running services.
- Deployment or release promotion while Zeus is unavailable.

## Acceptance

- A captured oversized request is rejected before transport, compacted, rebuilt,
  and dispatched under budget.
- Duplicate persisted turns and tool results are not reintroduced into model
  context.
- Historical tool output is bounded before the normal compression threshold.
- One task survives four in-place compactions, restart reconstruction, model
  fallback, and summary failure without losing its checkpoint.
- Every provider path uses the same resolved budget and admission guard.
- No retry repeats a completed tool side effect.
- Compression exhaustion never creates an empty continuation.

