# Selector-Managed Asset Cache Version Implementation Plan

> **For the implementer:** Execute this plan task by task. Do not combine the
> live cutover with unrelated offline/BFCache/sidebar or `ctl.sh` work.

**Goal:** Give selector-managed Hermes WebUI releases a unique browser
asset/service-worker cache identity and prove it on the live managed service.

**Architecture:** Keep `WEBUI_VERSION` as the product/update version. Add a
process-constant `WEBUI_ASSET_VERSION` that equals the selector-validated full
manifest SHA-256 in selector mode and otherwise equals `WEBUI_VERSION`. Inject
only the asset constant into app-shell, login, and service-worker cache keys.

**Tech stack:** Python standard library HTTP server, vanilla JavaScript service
worker, pytest through `./scripts/test.sh`, GitNexus, immutable release selector.

**Approved design:**
`docs/superpowers/specs/2026-07-27-selector-managed-asset-cache-version-design.md`

**Release base:** Exact running r64 source commit
`fdb5cf8fb4cdcf212b8e788d298ba42fab8aef30`.

**Impact warning:** GitNexus reports `CRITICAL` transitive risk for
`api.routes._render_index_shell_base` (8 direct / 238 total) and
`api.routes.handle_get` (139 direct / 231 total). The implementation therefore
changes only version-token imports/values inside those symbols and gates
deployment on focused route tests plus the full suite.

---

## Task 1: Add the selector asset-version resolver contract

**Files:**

- Create: `tests/test_selector_asset_cache_version.py`
- Modify: `api/updates.py`

### Step 1: Write failing resolver tests

Add tests that call a new `_detect_webui_asset_version(product_version)`
helper while controlling the two selector environment variables with
`monkeypatch`.

Pin these cases:

```python
VALID_A = "a" * 64
VALID_B = "b" * 64

def test_selector_asset_version_is_exact_manifest_digest(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_LAUNCH_MODE", "selector")
    monkeypatch.setenv("HERMES_WEBUI_MANIFEST_SHA256", VALID_A)
    assert updates._detect_webui_asset_version("unknown") == VALID_A

def test_selector_rebuilds_with_same_product_version_get_distinct_exact_tokens(...):
    ...

@pytest.mark.parametrize("value", ["", "A" * 64, "a" * 63, "g" * 64])
def test_selector_mode_fails_closed_on_invalid_manifest_digest(...):
    with pytest.raises(RuntimeError, match="selector asset cache identity"):
        updates._detect_webui_asset_version("unknown")

def test_non_selector_mode_preserves_product_version(...):
    ...
```

Also assert the exported `WEBUI_ASSET_VERSION` is non-empty in the ordinary
test environment.

### Step 2: Run the tests and observe RED

Run:

```bash
./scripts/test.sh tests/test_selector_asset_cache_version.py -q
```

Expected: failure because `_detect_webui_asset_version` and
`WEBUI_ASSET_VERSION` do not exist.

### Step 3: Implement the minimal resolver

In `api/updates.py`, add a compiled strict digest regex and:

```python
def _detect_webui_asset_version(webui_version: str) -> str:
    if os.environ.get("HERMES_WEBUI_LAUNCH_MODE") != "selector":
        return webui_version
    manifest_sha256 = os.environ.get("HERMES_WEBUI_MANIFEST_SHA256", "")
    if not _SELECTOR_MANIFEST_SHA256_RE.fullmatch(manifest_sha256):
        raise RuntimeError(
            "selector asset cache identity is missing or invalid"
        )
    return manifest_sha256
```

Resolve constants once, in this order:

```python
WEBUI_VERSION = _detect_webui_version()
WEBUI_ASSET_VERSION = _detect_webui_asset_version(WEBUI_VERSION)
AGENT_VERSION = _detect_agent_version()
```

Do not include the rejected environment value in the exception.

### Step 4: Run the resolver tests and observe GREEN

Run:

```bash
./scripts/test.sh tests/test_selector_asset_cache_version.py -q
```

Expected: all tests pass.

---

## Task 2: Pin exact HTTP-boundary propagation

**Files:**

- Modify: `tests/test_static_asset_resolver.py`
- Modify: `tests/test_index_shell_template_cache.py`
- Modify: `tests/test_pwa_manifest_sw.py`
- Modify: `tests/test_sprint19.py`

### Step 1: Add failing served-byte tests

In `tests/test_static_asset_resolver.py`, monkeypatch
`api.updates.WEBUI_ASSET_VERSION` to one known 64-character digest and reset
`routes._INDEX_SHELL_CACHE`.

Prove exact output for:

- `_render_index_shell_base()` asset query tokens;
- `GET /login` containing
  `static/login.js?v=<exact-digest>`;
- `GET /sw.js` containing
  `hermes-shell-<exact-digest>` and `?v=<exact-digest>` for every resolved
  versioned shell asset; and
- no `unknown` or unresolved `__WEBUI_VERSION__` token in those responses.

Update the byte-equivalence oracle in
`tests/test_index_shell_template_cache.py` to use
`WEBUI_ASSET_VERSION`.

Update source-contract assertions in `tests/test_pwa_manifest_sw.py` and
`tests/test_sprint19.py` to require `WEBUI_ASSET_VERSION` in the three
cache-token substitution sites while continuing to require the existing
template placeholder shape.

### Step 2: Run the route tests and observe RED

Run:

```bash
./scripts/test.sh \
  tests/test_static_asset_resolver.py \
  tests/test_index_shell_template_cache.py \
  tests/test_pwa_manifest_sw.py \
  tests/test_sprint19.py -q
```

Expected: exact-digest assertions fail because the routes still inject
`WEBUI_VERSION`.

---

## Task 3: Route browser cache keys through the asset version

**Files:**

- Modify: `api/routes.py`
- Modify: `static/sw.js` (comments only)

### Step 1: Make the three surgical substitutions

In `api.routes._render_index_shell_base`, import and URL-quote
`WEBUI_ASSET_VERSION` instead of `WEBUI_VERSION`.

Inside `api.routes.handle_get`, do the same only in:

- the `/login` branch; and
- the `/sw.js` branch.

Do not change the `/api/settings` branch: it must continue exposing
`WEBUI_VERSION` as the product version.

Update nearby comments and the top comments in `static/sw.js` so they describe
a deployment asset identity rather than claiming the key is always Git-derived.
Keep the `__WEBUI_VERSION__` placeholder itself unchanged.

### Step 2: Run the route tests and observe GREEN

Run the Task 2 command again.

Expected: all tests pass, including byte-equivalence and exact served-token
checks.

### Step 3: Prove product-version consumers stayed separate

Run:

```bash
./scripts/test.sh \
  tests/test_updates.py \
  tests/test_update_channels.py \
  tests/test_version_badge.py \
  tests/test_model_cache_metadata.py \
  tests/test_issue1633_models_cache_version_stamp.py \
  tests/test_issue_windows_git_version_detection.py -q
```

Expected: all product/update/model-cache version tests pass unchanged.

---

## Task 4: Document and verify the complete code change

**Files:**

- Modify: `ARCHITECTURE.md`

### Step 1: Document the two identities

Add a short architecture note:

- `WEBUI_VERSION` is product/update/display identity;
- `WEBUI_ASSET_VERSION` is browser asset/service-worker cache identity;
- selector mode requires the full lowercase release-manifest SHA-256 and fails
  before bind if it is invalid;
- non-selector modes retain existing `WEBUI_VERSION` cache behavior.

Do not edit `CHANGELOG.md`.

### Step 2: Run focused verification

Run:

```bash
./scripts/test.sh \
  tests/test_selector_asset_cache_version.py \
  tests/test_pwa_manifest_sw.py \
  tests/test_service_worker_api_cache.py \
  tests/test_static_asset_resolver.py \
  tests/test_static_asset_compression_and_cache.py \
  tests/test_index_shell_template_cache.py \
  tests/test_sprint19.py \
  tests/test_issue_windows_git_version_detection.py \
  tests/test_updates.py \
  tests/test_update_channels.py \
  tests/test_version_badge.py \
  tests/test_model_cache_metadata.py \
  tests/test_issue1633_models_cache_version_stamp.py -q
```

Expected: all pass.

### Step 3: Run lint and the full regression suite

Run:

```bash
./scripts/test.sh
```

Then run the repository diff-scoped ruff gate using the worktree virtual
environment:

```bash
.venv/bin/python scripts/ruff_lint.py --diff fdb5cf8fb4cdcf212b8e788d298ba42fab8aef30
```

Expected: full suite passes and no changed-line ruff errors.

If the known pre-existing sidebar harness failure appears, report it separately;
do not repair it in this logical change.

### Step 4: Review impact before commit

Run GitNexus against this exact worktree:

```text
detect_changes(scope="compare",
               base_ref="fdb5cf8fb4cdcf212b8e788d298ba42fab8aef30",
               worktree="<exact worktree path>")
```

Inspect every changed symbol and affected flow. Stop if any changed file is
outside the plan.

### Step 5: Commit the implementation

```bash
git add \
  api/updates.py \
  api/routes.py \
  static/sw.js \
  tests/test_selector_asset_cache_version.py \
  tests/test_static_asset_resolver.py \
  tests/test_index_shell_template_cache.py \
  tests/test_pwa_manifest_sw.py \
  tests/test_sprint19.py \
  ARCHITECTURE.md
git commit -m "fix: version selector-managed browser caches"
```

---

## Task 5: Build and attest immutable r65 offline

**Files:**

- No tracked source edits.
- Create private temporary JSON/test receipt files outside the repository.

### Step 1: Freeze pre-deployment truth

Read and record:

- current `/health` build identity, active runs/streams, PID, and listener;
- selector generation/current/candidate/last-good;
- exact current r64 manifest hash and release record;
- candidate commit/tree and clean Git status; and
- restart-boundary log offsets.

The expected rollback target must remain r64.

### Step 2: Prepare private release receipts

Create a mode-`0700` temporary receipt directory. Reuse the selector,
interpreter, sealed runtime, and sealed Agent identities already attested by
the r64 manifest, after re-verifying each current path/hash.

Create release metadata covering every path changed from the exact r64 base:

- one `ship` patch decision with rationale per changed path;
- SHA-256 receipts for focused tests, full suite, and ruff; and
- SHA-256 hashes for every changed runtime artifact and test file.

No credentials, state payloads, or conversation content enter the receipts.

### Step 3: Build the immutable candidate

Use `scripts/webui_release_cutover.py build` with:

- `--repo` set to this clean worktree;
- `--ref` set to the implementation commit;
- `--base-ref` and `--expected-base-commit` set to exact r64;
- a new immutable r65 build ID;
- the verified public origin URL;
- exact `--allowed-changed-path` entries from `git diff --name-only r64..r65`;
- the already-attested selector/interpreter/runtime/Agent identities; and
- the private metadata receipt.

Capture the returned candidate release path, commit, tree, record, and
manifest SHA-256. That returned manifest digest is the authoritative expected
browser asset version.

### Step 4: Verify candidate without activation

Run `scripts/webui_release_cutover.py verify-release` against the r65 path and
expected manifest digest. Read the manifest back and prove:

- it is immutable and internally hash-consistent;
- its commit/tree equal the tested implementation commit/tree;
- its base is exact r64;
- its changed paths equal the admitted set; and
- the selector/runtime/Agent identities still match.

Do not stage the selector if any proof fails.

---

## Task 6: Drain, activate, and accept r65 live

**Files:**

- No tracked source edits.
- Mutates only the already-authorized selector state and launchd-owned WebUI
  process.

### Step 1: Stage the candidate with CAS generation

Re-read selector state under its lock immediately before mutation. Require:

- `current == last_good == r64`;
- `candidate is null`; and
- generation equals the recorded precondition.

Stage r65 with a fresh transaction ID and the exact build record using
`state-stage --expected-generation <generation>`.

### Step 2: Drain live work

Poll `/health` once per second. Require `active_runs == 0` and
`active_streams == 0` continuously for 30 seconds. Reset the timer on any
activity. Abort and unstage/rollback rather than interrupting work if the
maintenance window expires.

### Step 3: Activate and restart

Re-read state/generation, run `state-activate` with CAS, then restart only the
existing launchd WebUI job. Do not invoke `ctl.sh`.

### Step 4: Apply the bounded acceptance gate

For at most 60 seconds, polling once per second with a three-second HTTP
deadline, require:

- new PID and listener ownership;
- `/health.status == "ok"`;
- managed r65 build ID, candidate commit/tree, and exact manifest digest;
- admission open with zero startup error;
- `/login` contains exactly
  `static/login.js?v=<candidate-manifest-sha256>`;
- `/sw.js` contains exactly
  `hermes-shell-<candidate-manifest-sha256>`;
- every resolved versioned shell asset in `/sw.js` uses exactly
  `?v=<candidate-manifest-sha256>`;
- neither response contains `unknown` or `__WEBUI_VERSION__`; and
- restart-boundary logs contain no startup/cache-identity failure.

### Step 5: Promote or rollback

If every condition passes, re-read selector state and promote r65 with
`state-promote --expected-generation <generation>`. Confirm
`current == last_good == r65` and `candidate is null`.

If any condition fails or times out:

1. re-read selector state;
2. atomically select r64 with `state-rollback` using the current generation;
3. restart the same launchd job; and
4. apply the same bounded 60-second identity/health read-back to r64.

A rollback read-back failure remains an explicit failed deployment.

---

## Task 7: Final acceptance receipt

Record:

- r64 pre-cutover and r65 post-cutover build/manifest/commit/tree identities;
- focused/full test and lint receipt hashes;
- GitNexus `detect_changes` result;
- drain interval and selector generation transitions;
- old/new PID and listener receipts;
- exact `/login` asset token and `/sw.js` cache namespace;
- restart-boundary log result; and
- the verified rollback command/target.

Confirm the original main checkout still contains only the user’s pre-existing
`AGENTS.md` modification and that no local-only release paths or private
receipts were committed.
