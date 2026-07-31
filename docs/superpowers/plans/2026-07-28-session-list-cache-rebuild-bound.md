# Session List Cache Rebuild Bound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound stable `/api/sessions` projection rebuilds to once per 30 seconds while preserving immediate source- and event-driven invalidation.

**Architecture:** Keep the existing cache, source stamp, event invalidation, and runtime overlay unchanged. Change only the age-based safety backstop used by idle and streaming cache entries, and prove the five-second client activity poll reads cached data while entries still expire at the 30-second bound.

**Tech Stack:** Python 3.11, pytest, Ruff, managed Hermes WebUI selector release controller

---

### Task 0: Prove the pre-edit blast radius

**Files:**
- Read only: `api/route_session_list_cache.py`

- [x] **Step 1: Run the mandatory upstream impact analysis**

Run before editing:

```text
impact(target="_session_list_cache_get",
       direction="upstream",
       file_path="api/route_session_list_cache.py",
       includeTests=true,
       maxDepth=3,
       minConfidence=0.8,
       repo="/Users/seb/hermes-webui")
```

Observed on 2026-07-28: `LOW` risk, zero indexed direct callers, and zero
indexed affected processes. Because the route binds cache helpers dynamically,
the empty graph is not treated as proof of no impact; the focused cache,
projection, and long-history suites remain mandatory.

### Task 1: Add the failing idle-cache contract

**Files:**
- Modify: `tests/test_session_sidebar_cache.py:844-915`
- Test: `tests/test_session_sidebar_cache.py`

- [ ] **Step 1: Replace the stale idle-TTL regression with the required behavior**

Add a test with this contract:

```python
def test_idle_cache_spans_activity_poll_but_expires_at_convergence_bound(monkeypatch):
    routes._session_list_cache_clear()
    key = _cache_policy_key()
    monkeypatch.setattr(
        routes,
        "_session_list_cache_source_stamp",
        lambda k: ("stable",),
    )
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    routes._session_list_cache_set(key, _session_cache_payload("idle"))
    _age_cache_entry(key, 5.5)
    payload, fresh = routes._session_list_cache_get(key)
    assert fresh is True
    assert payload == _session_cache_payload("idle")

    routes._session_list_cache_set(key, _session_cache_payload("expired"))
    _age_cache_entry(key, 30.1)
    payload, fresh = routes._session_list_cache_get(key)
    assert payload is None
    assert fresh is False
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
HERMES_WEBUI_TEST_PORT=27268 \
HERMES_WEBUI_TEST_STATE_DIR=/private/tmp/hermes-r67-red-20260728 \
./scripts/test.sh -q \
  tests/test_session_sidebar_cache.py::test_idle_cache_spans_activity_poll_but_expires_at_convergence_bound
```

Expected: FAIL because the stable entry aged 5.5 seconds is older than r66's
2.5-second idle TTL.

### Task 2: Implement the single cache-policy change

**Files:**
- Modify: `api/route_session_list_cache.py:16-26`
- Modify: `tests/test_session_sidebar_cache.py:844-915`

- [ ] **Step 1: Set one 30-second age bound for idle and streaming entries**

Use:

```python
_SESSIONS_CACHE_TTL_SECONDS = 30.0
_SESSIONS_CACHE_STREAMING_TTL_SECONDS = 30.0
```

Update the nearby comments to identify
`static/sessions.js` `_sessionActivityPollMs = 5000` as the idle trigger and
describe age expiry as a bounded safety backstop. Do not change source-stamp
calculation, event invalidation, client polling, or runtime overlays.

- [ ] **Step 2: Update stale test comments**

Rename the test helper `_streaming_ttl_key` to `_cache_policy_key`. Replace
`test_streaming_widens_cache_freshness_window`, whose differential 2.5/10
second contract is intentionally obsolete, with a unified-policy contract:
an entry aged 29.9 seconds is fresh both idle and streaming, and an entry aged
30.1 seconds is stale both idle and streaming. Retain the separate
`test_streaming_window_still_evicts_past_streaming_ttl` bounded-eviction
coverage, but update its name/docstring to describe the shared convergence
bound. Remove every comment that promises a 2.5-second idle TTL or a 10-second
streaming TTL.

- [ ] **Step 3: Run the new test and verify GREEN**

Run the exact Task 1 command with a fresh state directory and port.

Expected: `1 passed`.

- [ ] **Step 4: Run focused regression suites**

Run:

```bash
HERMES_WEBUI_TEST_PORT=27269 \
HERMES_WEBUI_TEST_STATE_DIR=/private/tmp/hermes-r67-green-20260728 \
./scripts/test.sh -q \
  tests/test_shared_state_db_session_projection.py \
  tests/test_session_sidebar_cache.py \
  tests/test_session_list_long_history_perf.py
```

Expected: all collected tests pass, with only the existing platform skips.

- [ ] **Step 5: Run diff and lint checks**

Run:

```bash
git diff --check
.venv/bin/python scripts/ruff_lint.py --diff \
  286139e9a9ea9e14d29aa57095eba44c4f9897b3
```

Expected: both commands exit zero.

- [ ] **Step 6: Run the mandatory GitNexus change gate**

Run:

```text
detect_changes(scope="compare",
               base_ref="286139e9a9ea9e14d29aa57095eba44c4f9897b3",
               worktree="/Users/seb/hermes-webui/.codex-repair/session-list-cache-r67")
```

Require only the cache-policy helper/test symbols and expected session-list
flows. Stop if another production path appears.

- [ ] **Step 7: Commit the implementation**

```bash
git add api/route_session_list_cache.py tests/test_session_sidebar_cache.py
git commit -m "fix: bound session-list projection rebuilds"
```

### Task 3: Prepare exact r67 release artifacts

**Files:**
- Read: `scripts/webui_release_cutover.py`
- Create outside Git: immutable managed r67 release snapshot, manifest, release
  plan, transaction journal, focused test receipt, and a reverse r66 cutover
  template

- [ ] **Step 1: Verify source identity and cleanliness**

Require:

- branch `codex/session-list-cache-r67`;
- clean worktree;
- parent chain includes accepted r66 commit `286139e9`;
- the implementation commit changes only the cache helper and its focused test
  beyond the committed design/plan documentation.

- [ ] **Step 2: Build and attest r67**

After the focused tests and Ruff gate, calculate their receipt SHA-256 values.
Use `apply_patch` to create
`/private/tmp/hermes-r67-release-<R67_COMMIT>/r67-metadata.json` with one
`ship` decision for each of the four paths changed from r66, the two receipt
hashes, and artifact hashes for the cache helper and test. Validate it with
`jq -e .`.

Then run the existing builder (with `<R67_COMMIT>` replaced by the frozen
implementation commit):

```bash
MANAGED_PY=/Users/seb/.local/share/hermes-live-reliability-20260723/runtimes/snapshots/e6f0a29ce02a08f6693f08eacd0a79fd97ed6c870d6c77b5cdf5478fae5024d8/python-home/bin/python3.11

"$MANAGED_PY" scripts/webui_release_cutover.py build \
  --repo /Users/seb/hermes-webui/.codex-repair/session-list-cache-r67 \
  --ref <R67_COMMIT> \
  --release-root /Users/seb/.local/share/hermes-live-reliability-20260723/selector/releases \
  --build-id hermes-candidate-20260728-r67 \
  --base-ref 286139e9a9ea9e14d29aa57095eba44c4f9897b3 \
  --expected-origin-url https://github.com/nesquena/hermes-webui.git \
  --expected-base-commit 286139e9a9ea9e14d29aa57095eba44c4f9897b3 \
  --allowed-changed-path api/route_session_list_cache.py \
  --allowed-changed-path tests/test_session_sidebar_cache.py \
  --allowed-changed-path docs/superpowers/specs/2026-07-28-session-list-cache-rebuild-bound-design.md \
  --allowed-changed-path docs/superpowers/plans/2026-07-28-session-list-cache-rebuild-bound.md \
  --selector /Users/seb/.local/share/hermes-live-reliability-20260723/control/webui_release_selector.py \
  --interpreter "$MANAGED_PY" \
  --selector-identity-json /Users/seb/.local/share/hermes-live-reliability-20260723/private/identities/r53-selector-identity.json \
  --interpreter-identity-json /Users/seb/.local/share/hermes-live-reliability-20260723/private/identities/r53-interpreter-identity.json \
  --runtime-identity-json /Users/seb/.local/share/hermes-live-reliability-20260723/private/identities/r52-runtime-identity.json \
  --agent-source-identity-json /Users/seb/.local/share/hermes-live-reliability-20260723/private/identities/r64-agent-source-identity.json \
  --metadata-json /private/tmp/hermes-r67-release-<R67_COMMIT>/r67-metadata.json
```

Capture the command's single JSON stdout object from the tool result. Use
`apply_patch`—not shell redirection—to persist it byte-for-byte as
`/private/tmp/hermes-r67-release-<R67_COMMIT>/build-r67.json`, then validate
it with `jq -e` and run `verify-release` against the emitted release path and
manifest. Record the emitted source commit, tree, manifest SHA, runtime manifest
SHA, Agent identity, and exact r66 rollback identity before touching live
selector state.

- [ ] **Step 3: Generate and inspect the r67 transaction plan**

Create `/private/tmp/hermes-r67-prepare.py` with `apply_patch`. The preparer
must use `private/live-cutover-plan-53-r66.json` as its schema-complete
template, refuse unless selector generation/current/last-good are exactly
`160/r66/r66` with candidate and pending transaction null, select the first
unused numeric transaction suffix, and create only new transaction-scoped
paths. It must:

- derive a schema-complete transaction candidate identity from `build-r67.json`
  and the field schema in `candidate-r66-tx53.json`; require every immutable
  WebUI/runtime/Agent path and hash to equal the build receipt, set
  `launchd_label=com.parantoux.hermes-webui`, `startup_fenced=true`, set the
  newly allocated `startup_transaction_id`, and set `selector_generation` to
  the exact post-stage/post-activate generation (`pre_generation + 2`);
- persist that derived identity as a new
  `candidate-r67-tx<suffix>.json`, validate every required key against
  `_load_cutover_plan`, and point `expected_candidate_identity_json` at it;
- copy the current installed WebUI/gateway plists into new rollback paths;
- copy the r66 staged WebUI/gateway plists as transformation templates;
- allocate fresh staged, snapshot, quarantine, CLI-shim, journal, ingress-token,
  and ingress-receipt paths;
- retain the attested selector/runtime/Agent/watchdog identities; and
- emit both the plan and a manifest of every created path.

Run:

```bash
"$MANAGED_PY" /private/tmp/hermes-r67-prepare.py \
  --mode forward \
  --build-identity /private/tmp/hermes-r67-release-<R67_COMMIT>/build-r67.json \
  --output /private/tmp/hermes-r67-release-<R67_COMMIT>/live-cutover-plan-r67.json
"$MANAGED_PY" scripts/webui_release_cutover.py inspect-plan \
  --plan /private/tmp/hermes-r67-release-<R67_COMMIT>/live-cutover-plan-r67.json
```

The preparer is release scaffolding, not product code; review its complete
source and generated diff against the r66 plan before continuing.

- [ ] **Step 4: Re-run the focused suite against the frozen release source**

Persist the command, output, exit code, and hashes under `/private/tmp`.

### Task 4: Managed cutover and automatic acceptance

**Files:**
- Read/write through controller:
  `/Users/seb/.local/share/hermes-live-reliability-20260723/selector/`
  and its private transaction directory

- [ ] **Step 1: Prove the live preconditions**

Require r66 as both current and last-good, no candidate or pending transaction,
exact WebUI/gateway pair identity, open admission, absent pair gate, watchdog
scheduled/idle, and no active release owner.

- [ ] **Step 2: Run the existing release controller**

Use `scripts/webui_release_cutover.py release-commit` with the generated r67
plan. Do not manually edit selector state, launchd plists, or transaction
journals.

- [ ] **Step 3: Verify terminal release receipts**

Require exact r67 WebUI and gateway identities, open admission, absent pair
gate, current and last-good r67, candidate and pending transaction null, CLI
link on r67, watchdog restored, and transaction phases terminal.

- [ ] **Step 4: Define and prove exact post-promotion rollback**

Do not use public `state-rollback` after promotion: r67 is then also
`last_good`. Instead, use the same reviewed preparer in reverse mode to make a
new managed release transaction targeting the existing immutable
`hermes-candidate-20260727-r66` release:

```bash
"$MANAGED_PY" /private/tmp/hermes-r67-prepare.py \
  --mode reverse \
  --target-build hermes-candidate-20260727-r66 \
  --output /private/tmp/hermes-r67-release-<R67_COMMIT>/live-cutover-plan-rollback-r66.json
"$MANAGED_PY" scripts/webui_release_cutover.py inspect-plan \
  --plan /private/tmp/hermes-r67-release-<R67_COMMIT>/live-cutover-plan-rollback-r66.json
```

Reverse mode must refuse unless r67 is terminally promoted with no candidate or
pending transaction. It derives a fresh expected startup generation and
transaction id while preserving the existing r66 release path, build id,
commit, tree, WebUI manifest, runtime manifest, and Agent manifest. The reverse
transaction therefore changes only unavoidable control-plane generation/startup
metadata and restores the exact r66 application/gateway/CLI pair. Generate and
inspect this reverse plan immediately after r67 promotion, before starting the
two performance windows. Do not run `release-commit` for this reverse plan
unless a later identity, scheduler, or performance gate fails.

- [ ] **Step 5: Follow through on the watchdog**

Wait for an automatic scheduled run. Require persisted cron output/state, an
idle recovery state, no manual action required, no active lease/slot, and a
future next run. A merely scheduled check does not satisfy this step.

- [ ] **Step 6: Measure performance and decide**

After release work and watchdog activity settle, collect two consecutive
60-second CPU windows at one-second resolution and matching access-log latency
windows. Accept only when both CPU p95 values are below 80%, combined
`/api/sessions` p95 is below one second, and zero calls exceed five seconds.

If any threshold, identity check, or scheduler check fails, invoke the
pre-inspected managed reverse plan to exact r66:

```bash
"$MANAGED_PY" scripts/webui_release_cutover.py release-commit \
  --plan /private/tmp/hermes-r67-release-<R67_COMMIT>/live-cutover-plan-rollback-r66.json
```

Verify rollback terminally. Do not leave r67 active under an observe-only
state.
