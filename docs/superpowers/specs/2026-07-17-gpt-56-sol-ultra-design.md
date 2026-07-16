# GPT-5.6 Sol Ultra reasoning mode

## Goal

Allow Hermes One to expose an `Ultra` reasoning option for GPT-5.6 Sol. The
OpenAI wire value is `max`; `Ultra` is the Hermes One display label.

## Scope

- Recognize `max` as a valid reasoning-effort value in the Hermes One request
  and configuration path.
- Show `Ultra` only when the active model is GPT-5.6 Sol (`gpt-5.6-sol` or the
  `gpt-5.6` alias).
- Persist the selected setting as `agent.reasoning_effort: max` and send it as
  `reasoning_effort: "max"` through the existing request path.
- Keep existing reasoning levels, defaults, and behavior unchanged for other
  models.

## Non-goals

- No new provider or endpoint.
- No global `max` option for models that may reject it.
- No silent downgrade when a stored `max` setting is used with another model.

## Implementation shape

Use the existing reasoning-effort hook and request builder. Extend the shared
effort type/parser to include `max`, add a model predicate for GPT-5.6 Sol,
and have the selector derive its options from the active model. The display
label remains localized through the existing i18n mechanism if the selector
uses translated labels; otherwise the English fallback is `Ultra`.

## Error handling

The request layer continues to pass the selected value through unchanged. If a
non-Sol model has a stale stored `max` value, the UI must not offer `Ultra` and
the model-aware selection logic should fall back to the model's normal default
rather than sending an unsupported value.

## Verification

- Add focused tests for Sol model matching and `max` request/config behavior.
- Add a regression test that non-Sol models do not expose `Ultra`.
- Run the Hermes One focused test suite, typecheck/lint commands defined by its
  package scripts, and `git diff --check`.
