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

Hermes Agent owns one admission check inside the final execution callback,
immediately before transport I/O. This covers tool-loop calls, retries, and
model fallbacks and sees the provider-ready request after request middleware,
execution middleware, transport preflight, prompt assembly, and tool assembly.

The guard performs one bounded sequence:

1. Measure the body-bearing fields of the final provider kwargs with the
   existing provider-payload estimator (`messages` or `input`, instructions,
   and tools).
2. Add the existing conservative estimator margin used by background-review
   admission: `max(1,024, ceil(estimated_input * 5%))`.
3. Admit only when that total is at or below the smaller of:
   - the compressor's resolved `threshold_tokens`; and
   - `context_length -` any explicit final-request output-token value.
4. If it does not fit, replace historical tool bodies and old tool-call
   arguments with the compressor's existing compact receipts.
5. If it still does not fit, run the existing compressor in place.
6. Rebuild and measure the request again.
7. Dispatch only when it fits; otherwise fail closed before network I/O.

`context_length`, `threshold_tokens`, threshold percentage, and configured
output reservation come only from the context compressor bound to the active
model/provider. Fallback must rebind that compressor before rebuilding the
request. A non-positive budget or an Agent/compressor model-provider identity
mismatch fails closed; the guard does not invent a second context-window
resolver or silently use a generic window.

The guard reuses the current estimator, deterministic tool pruning, compressor,
and in-place persistence. It is not a plugin, service, retrieval layer, or new
context framework.

### In-place continuity

Compaction keeps the same logical and physical session ID. The WebUI sidecar's
visible `messages` remain the user-facing transcript. Agent `state.db` owns
model context: `archive_and_compact()` atomically marks the previous active
rows `active=0, compacted=1` and inserts the compacted active rows. Restart
loads only `active=1`; archived rows remain searchable and recoverable.

The compacted model context is the existing structured compression summary plus
the compressor's protected tail. "Recent tail" means the existing
`tail_token_budget`, while `protect_last_n` remains the minimum protected
message count. A summary is bounded by the existing
`min(context_length * 5%, 10,000 tokens)` rule.

Cheap tool pruning is a compaction operation, not an ephemeral request-only
mutation: when it changes model history, the pruned active projection is
persisted through the same atomic in-place transaction before request rebuild.
Outside the protected tail, existing behavior summarizes tool bodies over 200
characters, truncates large tool-call arguments, and keeps only the newest full
copy of identical tool output. User and assistant text is never deduplicated by
content. Turn duplication is prevented by removing whole-database adoption and
re-baselining the existing post-compaction persistence cursor after the atomic
rewrite.

The legacy rotation/whole-database-adoption path is not used by automatic
compression. WebUI must never create an empty recovery conversation. If a
request remains irreducible after compaction, WebUI reports that specific
failure on the existing session and preserves the last valid bounded context.

### Failure behavior

- Provider fallback rebinds the compressor, rebuilds provider kwargs, and reruns
  admission. It does not reset the turn-scoped compression-attempt counter.
- The existing maximum of three compaction attempts applies across the whole
  logical turn and all providers. A provider context-limit rejection may retry
  once only when compaction measurably shrank the request, and consumes that
  same turn-scoped budget.
- Admission retries only the provider call. It never re-enters the tool
  executor. Completed assistant/tool pairs retain their provider
  `tool_call_id` when tool bodies become receipts, so prior side effects are
  not run again.
- Compression or summary failure preserves the previous valid model context;
  it never adopts a partial snapshot and never creates blank context.
- Admission telemetry records category sizes and decisions, not prompt content.

## State transitions

| Event | Required result |
| --- | --- |
| First or tool-loop dispatch | Build final kwargs, admit, then perform transport I/O. |
| Request above budget | Prune tools in place, rebuild, and remeasure. |
| Still above budget | Compress in place, rebuild, and remeasure within the three-attempt turn cap. |
| Provider fallback | Rebind context identity; rebuild and re-admit without resetting the cap. |
| Provider context rejection | Compact-and-retry once only after measurable shrinkage. |
| Compression failure | Keep the prior active rows and last valid WebUI context unchanged. |
| Restart | Load the one active compacted projection; archived rows never rejoin it. |
| Irreducible request | Fail on the existing session; create no recovery child. |

## Non-goals

- LCM, RAG, a vector database, or another context engine.
- A new model tool or additional provider schema.
- Redesigning Tool Search or the system prompt in this change.
- Editing live Hermes configuration, sessions, credentials, or running services.
- Deployment or release promotion while Zeus is unavailable.

## Acceptance

- A captured oversized request is rejected before transport, compacted, rebuilt,
  and dispatched only when `estimate + margin <= resolved ceiling`.
- Duplicate persisted turns and tool results are not reintroduced into model
  context.
- Tool output over 200 characters outside `tail_token_budget` /
  `protect_last_n` becomes an existing compact receipt before dispatch.
- One task survives four in-place compactions, restart reconstruction, model
  fallback, and summary failure while retaining exact fixture markers for its
  objective, constraints, completed work, and next action.
- Every provider path uses the same resolved budget and admission guard.
- No retry repeats a completed tool side effect.
- Compression exhaustion never creates an empty continuation.
