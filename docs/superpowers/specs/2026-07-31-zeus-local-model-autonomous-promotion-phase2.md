# Zeus Local-Model Autonomous Promotion — Phase 2

**Date:** 2026-07-31
**Status:** Deferred proposal — not part of MVP implementation or acceptance
**Depends on:** `2026-07-31-zeus-evidence-calibrated-webui-release-design.md`

## Purpose

Phase 2 allows a patch authored by whichever local model is active on Zeus to
enter production automatically for explicitly approved low-risk WebUI task
classes.

The release-actuator MVP must ship and produce reliable production receipts
before this phase is designed into an implementation plan. No model output,
shadow result, or MVP controller can enable Phase 2.

## Entry requirements

- The MVP actuator has completed trusted-ref releases and complaint rollbacks.
- Crash injection proves every activation and rollback boundary.
- Exact model, runtime, token, wall-time, and outcome telemetry exists for
  representative successful and failed shadow tasks.
- The operator has approved an independent Phase 2 policy authority.
- The model worker still lacks release-ref, signer, policy, and Mac authority.

## Additional controls

### Attested local inference broker

Replace the MVP's direct-process proof with a broker that:

- exposes one local Unix-socket interface;
- authenticates the caller with OS peer credentials;
- has one pinned local inference target and no general proxy behavior;
- denies external egress;
- issues a signed lease binding model weights, executable, arguments, context,
  caller, task, and resource policy;
- invalidates the lease on model or runtime change.

### Calibrated budgets

For each `{model identity, task class, stage}` record:

- logical, cached, and uncached input;
- output and context high-water mark;
- prompt/decode throughput, wall time, and measured or labelled-estimated GPU
  work;
- deterministic failure signature, repair outcome, review outcome, and release
  outcome.

Calibration is failure-inclusive. Interrupted, exhausted, malformed, and model
failures remain in the denominator; infrastructure failures are labelled
separately and still count against resource capacity.

Before fitting policy:

- freeze a content-addressed corpus;
- separate fitting and held-out partitions;
- declare task classes, sample floor, confidence method, completion target,
  non-interference target, and exhaustion behavior;
- retain immutable member lists and the derivation code.

The production allowance is the smallest measured resource envelope whose
failure-inclusive held-out lower bound meets the signed target. A model or
material runtime change invalidates the allowance and returns the class to
shadow.

### Model-authored task classes

The first class must have:

- an exact allowlist and forbidden surface manifest;
- deterministic observable acceptance tests;
- no authentication, security, dependency, migration, persistence, durability,
  Gateway/Agent, bootstrap, or release-controller changes;
- an independently signed promotion policy;
- a complaint rollback exercised before unattended enablement.

File count and line count remain anomaly signals, not semantic admission.

## Phase 2 workflow

```text
SHADOW_COMPLETE
  → POLICY_CLASSIFIED
  → BROKER_ATTESTED
  → BUDGET_ADMITTED
  → DETERMINISTICALLY_REVIEWED
  → READY
  → MVP_ACTUATOR
```

The Phase 2 controller may submit a model-authored commit to READY only when the
signed class policy, broker lease, deterministic gates, and held-out budget
policy all match. It still cannot sign or widen policy.

## Phase 2 acceptance

- broker peer, endpoint, egress-denial, lease, and model identity are proved;
- policy fitting is reproducible from immutable member lists and code;
- held-out failures remain visible in the denominator;
- model/runtime/class changes invalidate admission;
- shadow evidence cannot write production policy;
- a model cannot modify protected tests, policy, signer, publisher, release ref,
  or actuator;
- the first enabled class survives unattended release and complaint rollback;
- disabling Phase 2 leaves the MVP trusted-ref actuator fully usable.

## Explicitly unresolved until Phase 2 review

- independent policy authority;
- initial task class;
- sample floor and confidence method;
- completion and non-interference targets;
- measured resource envelope;
- stronger Mac account separation or privileged sealing.

These values must be selected from MVP evidence and an explicit operator risk
decision. They are intentionally not invented in the MVP design.
