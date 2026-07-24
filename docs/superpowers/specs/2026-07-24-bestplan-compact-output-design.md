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
resolved synthesizer identity. It never asks a child model to report which
models ran.

Attempt order matches configured scheduling order. A failed attempt that does
not prevent quorum remains visible with a sanitized host-owned reason:

```text
Models: glm (glm-5.2) ✓ · kimi-k3 (k3) ✗ provider_error · sol (gpt-5.6-sol) ✓
```

No provider exception text, credential material, endpoint, or raw response is
included.

## Compact body contract

The synthesis prompt requests:

- `TL;DR`: two to four concise bullets;
- `Next steps`: at most five short numbered actions;
- `Risks`: only material risks, at most three bullets, omitted when none.

The prompt explicitly rejects preambles, candidate-by-candidate narration,
repeated context, and a second model ledger. The host does not hard-truncate
the body because preserving an essential verification instruction is more
important than enforcing a byte limit.

## Receipts and failures

The version-2 receipt schema, durable JSONL record, hashes, reason codes, and
validation rules remain unchanged.

On success, `final_response` contains only the host-owned model ledger and the
compact body. The raw receipt is returned separately in the structured outcome
for internal consumers but is not concatenated into visible assistant text.

If durable receipt persistence fails after successful synthesis, the visible
response still includes the compact plan plus the existing static persistence
warning.

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
- A new structured receipt field may carry the visible run's receipt without
  placing it in `final_response`.

## Verification

Tests must prove:

1. visible success output contains no receipt markers or receipt JSON;
2. persisted receipt content remains byte-for-byte equivalent to the receipt
   metadata for the run;
3. model entries appear in scheduled order with configured model identity and
   terminal status;
4. the named synthesizer identity is explicit;
5. failed-but-nonfatal explorers show only allow-listed reason codes;
6. the synthesis prompt requests the compact body contract;
7. receipt-write warnings remain visible without exposing exception text.

No live configuration, credential, deployment, process restart, or activation
is part of this change.
