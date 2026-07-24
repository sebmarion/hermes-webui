# BestPlan Compact Output Design

**Date:** 2026-07-24

## Goal

Make successful BestPlan responses materially shorter and immediately show
which explorer models ran and which model synthesized the answer.

The visible response must not expose the raw version-2 receipt JSON. The full
receipt remains durably persisted for audit and reconciliation.

## Chosen approach

Use a host-owned presentation envelope plus a concise synthesizer prompt.

Prompt-only formatting is insufficient because a model can omit or hallucinate
runtime identity. Hard truncation is rejected because it can cut verification
steps or material risks. The host therefore owns model/status presentation,
while the synthesizer owns only the compact plan body.

## Visible success format

```text
Models: glm (glm-5.2) ✓ · kimi-k3 (k3) ✓ · sol (gpt-5.6-sol) ✓
Synthesizer: sol (gpt-5.6-sol) ✓

TL;DR
- ...

Next steps
1. ...

Risks
- ...
```

The host builds the two identity lines from terminal attempt metadata and the
resolved synthesizer identity. For each entry, it displays resolved
provider/model identity when resolution succeeded and falls back to configured
identity only when `resolved` is `null`. It never asks a child model to report
which models ran.

Every ledger token is rendered through one host-owned sanitizer. It permits
only ASCII letters, digits, spaces, `.`, `_`, `-`, `+`, `/`, `:`, and `@`;
replaces every other character, newline, control character, or Markdown
delimiter with `?`; collapses repeated whitespace; and truncates each token to
64 characters. Status reasons come only from the existing reason-code
allow-list. This prevents model/provider text from forging another ledger line
or injecting Markdown.

Attempt order matches configured scheduling order. A failed attempt that does
not prevent quorum remains visible with a sanitized host-owned reason:

```text
Models: glm (glm-5.2) ✓ · kimi-k3 (k3) ✗ provider_error · sol (gpt-5.6-sol) ✓
```

No provider exception text, credential material, endpoint, or raw response is
included.

## Compact body contract

The synthesis prompt and host validator require:

- `TL;DR`: two to four concise bullets;
- `Next steps`: at most five short numbered actions;
- `Risks`: only material risks, at most three bullets, omitted when none.

The body contains no preamble, extra headings, candidate-by-candidate
narration, repeated context, or second model ledger. Each non-empty content
line is at most 240 characters and the complete body is at most 2,000
characters.

The host validates the returned structure before exposing it. If the first
synthesis body is invalid, BestPlan performs one bounded reformat retry through
the same named synthesizer, supplying the invalid body as untrusted source
text. If the retry also fails validation, times out, or raises a provider
error, the run fails as `synthesizer_failed` and returns no plan body. The host
does not truncate a body because preserving an essential verification
instruction is more important than forcing an invalid plan through.

## Receipts and failures

The version-2 receipt schema, durable JSONL record, hashes, reason codes, and
validation rules remain unchanged.

On success, `final_response` contains only the host-owned model ledger and the
validated compact body. The structured outcome always contains a mandatory
`receipt` field holding the complete marker-wrapped version-2 receipt string.
Internal consumers use this field and must not parse `final_response`.

If durable receipt persistence fails after successful synthesis, the visible
response still includes the compact plan plus the existing static persistence
warning. The mandatory structured `receipt` remains present, while
`receipt_persisted` is `false`.

Terminal BestPlan failures continue to return no plan body. Their existing
sanitized error presentation remains unchanged; the durable failed receipt
continues to carry the full ordered attempt ledger.

## Compatibility

- Existing version-1 and version-2 receipt readers remain unchanged.
- Receipt persistence and reconciliation remain unchanged.
- Explorer scheduling, quorum, provider routing, K3 trust checks, and named
  synthesis remain unchanged.
- The structured `body`, `attempts`, `runtime`, `successes`, and `quorum`
  outcome fields remain available.
- Every terminal outcome created after canonical validation carries the
  marker-wrapped receipt in its mandatory structured `receipt` field without
  placing it in visible success output.

## Verification

Tests must prove:

1. visible success output, the finalized assistant message, and streamed chat
   contain no receipt markers or receipt JSON;
2. parsed canonical inner receipt JSON equals the persisted JSONL record;
3. model entries appear in scheduled order with resolved identity when
   available, configured fallback otherwise, and terminal status;
4. configured-versus-resolved identity drift is displayed truthfully;
5. the named synthesizer identity is explicit;
6. failed-but-nonfatal explorers show only allow-listed reason codes;
7. ledger tokens cannot inject newlines, control characters, or Markdown and
   obey the 64-character bound;
8. the synthesis prompt requests the compact body contract;
9. the host accepts only the exact heading/item/line/body bounds;
10. one invalid body causes exactly one bounded reformat retry;
11. a second invalid body fails without returning a plan;
12. successful and failed structured outcomes carry a marker-wrapped
    `receipt`, and internal consumers do not parse it from `final_response`;
13. receipt-write warnings remain visible without exposing exception text.

No live configuration, credential, deployment, process restart, or activation
is part of this change.
