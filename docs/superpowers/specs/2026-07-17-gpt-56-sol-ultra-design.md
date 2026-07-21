# GPT-5.6 Sol Ultra mode

## Goal

Expose both `Max` and `Ultra` for GPT-5.6 Sol without ever sending the Codex
control-plane value `ultra` through a provider request field that accepts only
model reasoning effort through `max`.

`Max` and `Ultra` are different modes:

- `Max` runs one model turn at maximum reasoning effort.
- `Ultra` runs through Codex app-server with maximum reasoning and Codex's
  proactive subagent orchestration.

Hermes must not present a relabelled `max` request as Ultra.

## Canonical state and boundaries

Hermes' shared/provider-facing reasoning-effort type remains:

`minimal | low | medium | high | xhigh | max`

`ultra` is not a provider reasoning-effort value. It must never enter the
shared effort enum, `agent.reasoning_effort`, a Hermes Agent Responses request,
or a Hermes Gateway request body.

Persist an Ultra selection as two separate facts:

```yaml
agent:
  reasoning_effort: max
  reasoning_mode: ultra
```

Selecting any ordinary effort, including `max`, clears `reasoning_mode`.
This keeps CLI and non-Codex consumers on their canonical effort ladder while
preserving the distinct product/runtime mode.

## Availability

Offer Ultra only when all of these are true:

- the provider resolves to `openai-codex`;
- the model is exactly `gpt-5.6-sol`, or the Hermes alias `gpt-5.6` that maps
  to `gpt-5.6-sol` for Codex app-server;
- the local Codex model catalog advertises `ultra` for that model, or the
  installed Codex version is known to support the same app-server contract;
- the Codex executable is available and the installed Hermes Agent exposes
  app-server plus per-turn model/effort controls, explicit multi-agent feature
  enablement for Ultra, and deterministic subprocess teardown;
- browser chat is using the in-process/native WebUI runtime.

The selector shows both `Max` and `Ultra`. Other models may still show `Max`
when their existing capability rules allow it, but they never show Ultra.

For compatibility with list-driven clients that build this selector directly
from `supported_efforts`, the reasoning-status response appends the selection
alias `ultra` only when `ultra_available` is true. This lets an older client
that ignores the separate availability field expose the choice without
changing the newer WebUI contract. It does not extend the shared effort enum
or model resolver: persisted and provider-facing effort remain canonical
through `max`.

Gateway-backed WebUI chat does not currently have a request-scoped Codex Ultra
control plane. It must reject an active Ultra mode with a clear error instead
of silently running Max.

## Request and runtime flow

The browser treats Ultra as a mode, not an effort token. A current client posts
`effort: "max"` plus `mode: "ultra"`. The API also accepts the legacy cached
client shape `effort: "ultra"` as ingress-only compatibility and immediately
canonicalizes it to the same two persisted fields. When that legacy request
omits both model and provider context, the API resolves the unique advertised
Ultra context (`gpt-5.6-sol` + `openai-codex`) before capability checks. A
partial or conflicting context remains fail-closed, and canonical
`effort: "max"` plus `mode: "ultra"` still requires explicit context.

For an ordinary turn, Hermes continues using its existing runtime and sends
the canonical effort.

For an effective Sol Ultra turn, the WebUI selects Hermes Agent's existing
`codex_app_server` runtime. The Codex app-server session receives the selected
model and `effort: "ultra"` explicitly through its per-session/per-turn
protocol. This value stays inside Codex's control plane; Codex owns the mapping
to maximum provider reasoning and proactive delegation.

Ultra starts that app-server subprocess with Codex's stable `multi_agent`
feature explicitly enabled. This makes the selected product mode authoritative
even if an ordinary Codex session disabled collaboration in global config.
Authentication is also owned by the Codex CLI/app-server (`codex login`); the
Hermes AIAgent constructor must not require or initialize a second raw OpenAI
client before app-server startup.

Do not mutate the user's global `~/.codex/config.toml` to make a WebUI turn
work. Model and effort overrides must be scoped to the Hermes/Codex app-server
session so concurrent Codex and Hermes conversations cannot race.

The existing Hermes `delegate_task` tool remains unchanged. It is not an
equivalent fallback for Ultra because exposing the tool does not guarantee
proactive delegation.

## Legacy and model-switch behavior

An already-persisted `agent.reasoning_effort: ultra` is poisoned legacy state.
At every read/dispatch boundary:

- for an eligible Sol/OpenAI-Codex context, interpret it as canonical effort
  `max` plus mode `ultra`;
- for every other context, treat it as invalid and fall back through the
  existing model-aware effort rules;
- never forward or re-persist the raw legacy value.

The YAML write boundary performs the same one-way cleanup, so changing an
unrelated setting cannot preserve poisoned raw `ultra` state. Ordinary CLI and
setup writers atomically update the effort and remove `reasoning_mode`, so an
explicit Max/lower choice outside the WebUI cannot leave Ultra active.
Profile endpoint/default-model writers use this same canonical save boundary.

Switching away from Sol makes Ultra ineffective and exposes the destination
model's normal model-aware effort. The stored `reasoning_mode: ultra` may remain
so switching back to Sol restores the user's explicit choice; it must not alter
non-Sol requests. Selecting another effort clears it.

## Failure behavior

- If Codex app-server or the advertised Ultra capability is unavailable, hide
  Ultra and reject stale/cached Ultra writes with an actionable error.
- If app-server startup or a turn fails, surface that failure. Do not retry the
  same turn through the raw Responses transport as Max because that changes the
  selected execution semantics and can duplicate side effects. Once an Ultra
  turn has been dispatched, do not automatically replay it through credential
  self-heal either; a user retry is the safe recovery boundary.
- Raw Responses transports reject `ultra` at their final outbound call sites
  even if it is injected through `reasoning_config`, request/execution
  middleware, auxiliary adapters, request overrides, or nested `extra_body`
  fields.
- A reused app-server thread sends explicit effort `none` when reasoning is
  disabled so a preceding Max/Ultra override cannot remain sticky.
- Gateway mode with Ultra selected fails closed with guidance to use native
  WebUI chat or select Max.
- Existing ordinary reasoning efforts and provider/model ceilings remain
  unchanged.

## Verification

Automated coverage must prove:

- the shared effort enum still ends at `max` and matches Hermes Agent;
- Sol and its alias expose canonical `max`, while Ultra availability is a
  separate capability;
- eligible status responses append the UI selection alias `ultra` to
  `supported_efforts`, while ineligible responses never advertise it;
- Sol displays distinct Max and Ultra options; non-Sol models never display
  Ultra;
- selecting Ultra posts mode plus canonical effort, persists only
  `reasoning_effort: max` and `reasoning_mode: ultra`, and selecting Max clears
  the mode;
- cached-client and persisted legacy `ultra` values canonicalize before any
  persistence or dispatch boundary;
- contextless legacy `effort: ultra` resolves only to the unique Sol/Codex
  Ultra context, while partial or conflicting context remains rejected;
- ordinary/native and both gateway request builders never emit `ultra`;
- raw transport and advanced-request overrides fail closed on injected Ultra;
- effective Ultra constructs an app-server agent and passes the exact selected
  model plus Codex control-plane effort `ultra`, explicitly enables Codex
  multi-agent tools, and relies on Codex CLI auth without editing global Codex
  configuration;
- Gateway-backed Ultra fails clearly instead of downgrading;
- cached agents are separated/retired when Max/Ultra mode changes;
- retired/closed agents close and clear their owned Codex subprocess session;
- session delete/clear/model-switch/truncate and uncached `/btw` teardown close
  owned Codex subprocesses without touching live in-flight agents;
- unrelated Settings and profile writes cannot re-persist legacy raw Ultra;
- CLI/setup ordinary effort writes clear stale Ultra mode atomically;
- focused WebUI and Hermes Agent suites, `git diff --check`, and change-impact
  review pass.

Live acceptance must verify a real Sol Ultra turn through Codex app-server,
observe Codex subagent/orchestration events when the task is parallelizable,
confirm there is no HTTP 400 and no outbound raw `reasoning.effort: ultra`, and
exercise the shared selector at desktop and mobile widths.
