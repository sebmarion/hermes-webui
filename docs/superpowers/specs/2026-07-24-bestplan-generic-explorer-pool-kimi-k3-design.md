# Generic BestPlan Explorer Pool with Kimi K3

## Goal

Make BestPlan consume a genuinely configurable bounded pool of SOTA explorer
models,
then add Kimi K3 as one explorer while keeping OpenAI Sol as the explicit
synthesizer.

The default three-explorer run should dispatch one task to each configured
explorer:

1. GLM
2. Kimi K3
3. OpenAI Sol

Sol remains the only synthesizer unless the operator changes the explicit
`synthesizer` setting.

## Current state

The deployed BestPlan implementation is only partly configurable:

- it reads a list from `bestplan.lanes`;
- it round-robins explorer work across that list;
- provider and model strings are resolved through the normal Hermes provider
  path;
- validation nevertheless requires exactly two entries named `glm` and `sol`;
- the synthesizer is chosen implicitly from reverse list order; and
- the active `~/.hermes/config.yaml` has no top-level `bestplan` block, so the
  compiled two-lane defaults are used.

The similarly named `delegation.lanes` mapping is a separate subsystem.
BestPlan must not silently consume or mutate delegation lanes.

## Non-goals

- Do not change `/bestplan` or `/bp` command syntax.
- Do not change the existing `2..5` explorer-count clamp or quorum formula.
- Do not give explorers write-capable tools.
- Do not make Kimi K3 the synthesizer.
- Do not restart WebUI, the gateway, or Hermes Agent as part of implementation
  or configuration. Activation is a separate, explicitly approved step.
- Do not store an API key in this repository, a design document, a receipt,
  logs, command output, or BestPlan lane metadata.
- Do not reuse `delegation.lanes` as BestPlan configuration.

## Canonical configuration

BestPlan gains a canonical `explorers` list and a required explicit
`synthesizer` reference:

```yaml
bestplan:
  enabled: true
  explorers:
    - name: glm
      provider: custom:neuralwatt
      model: glm-5.2
      api_mode: chat_completions
      reasoning_effort: high
    - name: kimi-k3
      provider: custom:kimi-code
      model: k3
      api_mode: chat_completions
      reasoning_effort: max
    - name: sol
      provider: openai-codex
      model: gpt-5.6-sol
      api_mode: codex_app_server
      reasoning_effort: ultra
  synthesizer: sol
  explorer_timeout: 180
  synthesizer_timeout: 180
  overall_timeout: 540
```

The canonical normalized schema is strict:

- `explorers` contains between one and five entries. Five matches the existing
  maximum effective explorer count, so no configured entry is permanently
  unreachable.
- Each entry is a mapping containing exactly `name`, `provider`, `model`,
  `api_mode`, and `reasoning_effort`.
- All five values are strings and have surrounding whitespace stripped.
- Names are lowercased after trimming and must match
  `[a-z0-9][a-z0-9_-]{0,63}`. Duplicate normalized names fail validation.
- Provider and model values retain case after trimming.
- `api_mode` is lowercased and must be one of `chat_completions`,
  `codex_responses`, `anthropic_messages`, `bedrock_converse`, or
  `codex_app_server`.
- `reasoning_effort` is lowercased and must be one of `none`, `minimal`, `low`,
  `medium`, `high`, `xhigh`, `max`, or `ultra`.
- `reasoning_effort: ultra` requires `api_mode: codex_app_server`.
- `api_mode: codex_app_server` requires provider `openai` or
  `openai-codex`.
- Unknown explorer keys fail validation rather than being silently ignored.

The canonical top-level keys are `enabled`, `explorers`, `synthesizer`,
`explorer_timeout`, `synthesizer_timeout`, and `overall_timeout`. `enabled`
must be a boolean and defaults to `true`. In an explicitly present canonical
`bestplan` block, `explorers` and `synthesizer` are required. The three timeout
keys are optional and default to `180`, `180`, and `540` seconds respectively.
Timeouts must be finite numbers: explorer and synthesizer timeouts are each in
`1..3600` seconds and the overall timeout is in `1..7200` seconds. The overall
timeout remains an independent hard cap and may be shorter than the sum of the
stage timeouts.

`synthesizer` must be a string. It is trimmed and lowercased with the same name
grammar as explorer names, then must reference exactly one normalized explorer.
Every trimmed `provider`, `model`, `api_mode`, and `reasoning_effort` value must
remain non-empty. Unknown canonical top-level keys fail validation; only the
legacy adapter accepts `lanes` and `runtime_route`.

Names are stable operator-facing identifiers, not provider-family enums. Kimi
is therefore named `kimi-k3`, not disguised as the legacy `glm` slot.

The `synthesizer` value must match exactly one configured explorer name.
Unknown or duplicate names fail validation before dispatch.

Secrets resolve through the existing profile-aware Hermes provider/auth path.
The BestPlan schema carries only provider and model identity plus non-secret
runtime controls.

## Backward compatibility

Legacy `bestplan.lanes` remains read-compatible indefinitely. It is accepted
only when `bestplan.explorers` is absent:

- `lanes` is normalized to `explorers`;
- the same strict normalized explorer-entry validation is applied;
- if legacy configuration omits `synthesizer`, the last legacy lane becomes
  the normalized synthesizer;
- this last-entry inference exists only inside the legacy adapter;
- if both `lanes` and `explorers` are present, validation fails as ambiguous
  before any model call;
- legacy `runtime_route` is accepted and ignored only by this adapter, with a
  fixed non-secret deprecation warning;
- receipts identify the normalized explorer names and the selected
  synthesizer.

For legacy input, `lanes` is required and is normalized as a one-to-five-entry
list; every formerly valid two-lane configuration therefore remains accepted.
`enabled` and all three timeout keys use the same canonical defaults when
absent. `synthesizer` is optional only for legacy input and defaults to the last
normalized lane. `runtime_route` is optional, ignored, and never copied into
canonical runtime state.

The compiled fallback becomes the same existing GLM/Sol pair expressed through
`explorers`, with `synthesizer: sol`. Existing installations without a
`bestplan` block therefore preserve behavior.

Version-1 receipt reading remains supported indefinitely. Canonical new runs
always write version 2. A checked-in version-1 fixture proves old conversation
receipts continue to validate; legacy last-entry synthesis inference is never
used for canonical `explorers` configuration.

## Dispatch and synthesis

For an effective explorer count `N`, the orchestrator cycles through the
bounded `explorers` pool in configuration order:

```text
explorer[i] = explorers[i modulo len(explorers)]
```

With the proposed three-model configuration and `/bestplan 3`, each model runs
once. Counts larger than the configured pool cycle deterministically; smaller
counts use the first `N` entries. Because the configured pool is bounded to
five and the execution count can be five, every configured entry is reachable.
A one-entry pool is valid: the minimum effective count of two schedules two
independent attempts against that same explorer identity, and the explicitly
named single entry also serves as synthesizer.

Only scheduled explorer entries and the named synthesizer are resolved. An
unavailable explorer becomes a failed candidate and participates in the
existing quorum calculation. There is no silent model substitution.

Before explorer requests are dispatched, the named synthesizer receives a
local preflight: provider resolution, required-credential presence, adapter
construction, and immediate adapter teardown. This does not make a network
request and therefore does not prove endpoint health. If local preflight fails,
BestPlan returns a version-2 failed receipt without spending explorer tokens.
A separate bounded authenticated provider probe is part of configuration-time
acceptance, not every BestPlan run.

The synthesizer continues to receive the read-only BestPlan toolset and the
successful candidate packet. Kimi K3 never becomes synthesizer merely because
of list order or another lane's failure.

If the explicit synthesizer later fails or times out after explorer work, there
is no fallback synthesizer and no substitute plan body. BestPlan returns
`status: failed`, persists a version-2 receipt containing the successful
explorer attempts plus the sanitized synthesizer failure, and presents a
non-secret failure message to the operator.

## Provider configuration for Kimi K3

Kimi K3 is registered through Hermes' existing custom OpenAI-compatible
provider mechanism:

- provider name: `custom:kimi-code`;
- API root: `https://api.kimi.com/coding/v1`;
- wire model: `k3`;
- API mode: Chat Completions / Hermes `chat_completions`;
- API key: stored only through Hermes' normal secret environment or credential
  store with restrictive file permissions.

Before persistence, a minimal authenticated model or chat probe must confirm
that the supplied credential belongs to the coding endpoint and accepts model
`k3`. The probe output must be reduced to status, resolved model identity, and
request success; it must never echo the credential or authorization header.

The key pasted into chat must be treated as exposed. Configuration may be
tested with it at the operator's direction, but the final handoff must require
rotation and revalidation of the replacement.

## Receipts and observability

Every new run persists this version-2 logical receipt shape:

```json
{
  "version": 2,
  "run_id": "opaque-id",
  "requested_count": 3,
  "effective_count": 3,
  "quorum_required": 2,
  "attempts": [
    {
      "index": 0,
      "strategy": "evidence-first",
      "explorer": "kimi-k3",
      "configured": {"provider": "custom:kimi-code", "model": "k3"},
      "resolved": {"provider": "custom:kimi-code", "model": "k3"},
      "status": "success",
      "reason_code": null
    }
  ],
  "synthesizer": {
    "name": "sol",
    "configured": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    "resolved": {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    "status": "success",
    "reason_code": null
  },
  "status": "completed",
  "reason_code": null,
  "body_sha256": "hex-digest"
}
```

`attempts` always contains one entry per scheduled invocation, ordered by
`index`, even though execution completes asynchronously. `resolved` is `null`
when runtime resolution never succeeded. `body_sha256` is `null` for failed
runs without a final plan body.

Attempt `status` values are `success`, `failed`, or `timeout`. Synthesizer
status additionally allows `not_started`; run status is `completed` or
`failed`. Both the run and each attempt/stage carry `reason_code`, which is
either `null` or one of the host-owned values `credential_unavailable`,
`runtime_invalid`,
`construction_failed`, `provider_error`, `timeout`, `candidate_invalid`,
`quorum_unavailable`, `synthesizer_failed`, or
`receipt_persistence_failed`, plus `overall_timeout` at run/stage scope. Raw
exception strings are never receipt data.

When quorum is unavailable, the receipt contains every scheduled attempt, sets
the synthesizer to `status: not_started` with
`reason_code: quorum_unavailable`, and sets the failed run's top-level
`reason_code` to the same value. If the overall deadline expires before
synthesis, the synthesizer is `not_started` and both stage and run use
`overall_timeout`. If the deadline expires during synthesis, synthesizer status
is `timeout`, the run fails with `overall_timeout`, and no plan body is
returned.

No API keys, authorization headers, cookies, raw provider responses, or
credential paths are recorded.

The visible deterministic receipt should disclose the explorer model identities
and synthesizer identity compactly, so an operator can prove that Kimi K3
actually participated rather than relying on requested-model intent.

Existing version-1 receipts remain readable indefinitely. The version-2 writer
is unconditional for new runs and the reader dispatches by the explicit
version field.

All BestPlan-visible and persisted errors pass through one centralized
sanitizer that maps internal failures to the fixed reason codes above. The
sanitizer is used by receipts, CLI validation, provider-probe summaries,
operator-facing failures, warnings, and BestPlan log events. It never includes
exception text, request headers, raw responses, URLs containing credentials, or
secret-file contents.

## Failure behavior

- Empty, non-list, or larger-than-five `explorers`: fail before model calls.
- Duplicate or malformed explorer names: fail before model calls.
- Missing provider/model/API mode/reasoning: fail before model calls.
- Unknown schema keys or invalid field types: fail before model calls.
- Unknown `synthesizer`: fail before model calls.
- Invalid Ultra/API-mode combination: fail before model calls.
- Unavailable explorer credential: record a redacted failed candidate and
  continue only if quorum remains possible.
- Synthesizer local-preflight failure: write a failed receipt and stop before
  explorer dispatch.
- Synthesizer network failure or timeout after explorers: write a failed
  receipt, return no plan body, and never select a fallback.
- Provider timeout: use the existing bounded timeout and teardown contract.
- Receipt write failure: do not corrupt the final answer; return the plan with
  an explicit non-secret receipt-persistence warning.

## Files and state layers

Expected implementation surfaces:

- Hermes Agent orchestration:
  `agent/bestplan_orchestrator.py`
- Hermes Agent tests:
  `tests/agent/test_bestplan_orchestrator.py`
- BestPlan inspection CLI:
  `hermes_cli/subcommands/bestplan.py`
- Installed BestPlan skill documentation:
  `~/.hermes/skills/software-development/bestplan/SKILL.md`
- Active global Hermes configuration:
  `~/.hermes/config.yaml`
- Active secret environment or credential store selected by the existing custom
  provider implementation

The code change belongs in the Hermes Agent source branch used by the managed
release pipeline. The active configuration and credential mutation are
separate local-state operations. Neither operation implicitly activates or
restarts a live service.

## Verification

Automated tests must prove:

1. arbitrary one-to-five-entry explorer lists are accepted;
2. one, two, three, and five distinct named explorers schedule correctly;
3. a one-entry pool cycles independent attempts under the count minimum;
4. pools larger than five, duplicate names, unknown keys, invalid types,
   invalid modes/efforts, and unknown synthesizers fail before child
   construction;
5. name normalization and duplicate detection are case-insensitive;
6. timeout bounds are exact;
7. legacy `lanes` normalizes without changing its previous dispatch order and
   legacy last-entry synthesis inference stays inside the adapter;
8. a checked-in version-1 receipt fixture remains readable;
9. all new receipts are version 2 and preserve invocation index order;
10. `/bestplan 3` over GLM/Kimi/Sol dispatches exactly one of each;
11. the named Sol lane is always used for synthesis regardless of explorer
   order or failures;
12. an unavailable Kimi explorer is recorded and governed by quorum without
   silent substitution;
13. a synthesizer local-preflight failure prevents explorer requests;
14. a synthesizer network failure after explorer completion returns no fallback
    body and persists a failed version-2 receipt;
15. provider/model receipts use actual resolved identities when available;
16. a sentinel API secret injected into fake credential errors is absent from
    receipts, CLI output, probe output, warnings, logs, and operator-facing
    failures; and
17. ordinary chat, `/moa`, and delegation lanes are unchanged.

Repository tests run through the required project test wrappers. Before any
commit, GitNexus impact analysis must be performed for every modified symbol
and `detect_changes()` must confirm only the intended BestPlan flows are
affected.

Live acceptance, after a separately approved activation window, must prove:

1. `hermes bestplan lanes` reports GLM, Kimi K3, and Sol plus
   `synthesizer: sol`;
2. a minimal direct Kimi probe resolves `k3` without exposing the key;
3. `/bestplan 3` completes with quorum;
4. the persisted receipt shows one actual Kimi `k3` explorer and Sol
   synthesis; and
5. no new provider, auth, timeout, or secret-leak errors appear after the
   verification boundary.

## Rollback

Configuration rollback removes the `kimi-k3` explorer and its custom provider
reference, restores the preceding secret-file backup, and re-runs read-only
validation. Code rollback restores the previous BestPlan schema support while
leaving historical receipts readable.

No active process is restarted or killed during rollback without explicit
operator approval.
