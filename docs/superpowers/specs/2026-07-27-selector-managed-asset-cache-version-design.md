# Selector-Managed Asset Cache Version Design

## Status

Approved for implementation and live selector-managed deployment.

## Problem

Selector-managed releases are immutable source snapshots without `.git`
metadata or a baked `api/_version.py`. The existing
`api.updates._detect_webui_version()` therefore resolves `WEBUI_VERSION` to
`unknown`.

The app shell, login page, and service worker currently reuse
`WEBUI_VERSION` as their static-asset cache stamp. Every selector-managed
release consequently serves URLs such as `static/login.js?v=unknown` and uses
the same `hermes-shell-unknown` CacheStorage namespace. A newer immutable
release can therefore inherit stale frontend assets from an older release.

`WEBUI_VERSION` also drives user-facing version and update behavior, so
replacing it with a release-manifest hash would conflate product versioning
with cache identity.

## Goals

- Give every selector-managed release an immutable, release-specific static
  asset and service-worker cache identity.
- Preserve existing `WEBUI_VERSION` product and update semantics.
- Preserve current Git-checkout, dirty-tree, baked-image, and Docker behavior.
- Prove the fix at the served HTTP boundary and after a real selector cutover.

## Non-goals

- Do not cache API responses.
- Do not change sidebar/session projection or offline data semantics.
- Do not add periodic service-worker polling or BFCache refresh behavior.
- Do not change `ctl.sh` process ownership reporting.
- Do not modify the external selector protocol or release-manifest schema.

## Selected approach

Introduce a process-constant `WEBUI_ASSET_VERSION` beside `WEBUI_VERSION`.

The resolver follows this order:

1. When `HERMES_WEBUI_LAUNCH_MODE` is `selector` and
   `HERMES_WEBUI_MANIFEST_SHA256` is exactly 64 lowercase hexadecimal
   characters, set `WEBUI_ASSET_VERSION` to that exact full digest.
2. When selector mode is declared but the digest is missing or malformed,
   fail startup before the server binds or serves traffic.
3. Otherwise return the already-resolved `WEBUI_VERSION`.

The selector already validates the release manifest before exec and exports
its SHA-256 digest. Using the full digest distinguishes rebuilt candidates even
when they share the same source commit. Requiring both selector mode and a
strict digest prevents unrelated environments from accidentally opting into
the managed-release path.

The existing `__WEBUI_VERSION__` template placeholder remains unchanged to
avoid widening the static-template contract. Its server-side substitution
changes to `WEBUI_ASSET_VERSION` only where the value is a browser cache key:

- the app-shell asset URLs and bundle stamp;
- the login-page script URL; and
- the served service-worker script, including its cache namespace and
  versioned shell-asset list.

User-facing settings, server version display, update checks, compare links,
and model-cache compatibility stamps continue to use `WEBUI_VERSION`.

## Failure behavior

Missing, malformed, or uppercase manifest identity in declared selector mode
raises a startup error before bind. A selector-managed release may never serve
with an untrusted or `unknown` cache token. Missing or malformed selector-only
environment values in every other launch mode fall back to `WEBUI_VERSION`,
preserving existing Git, Docker, and baked-image behavior.

No runtime file reads, hashing, network calls, or selector-state reads are
added. Both version constants resolve once at module import.

## Test design

Add regression tests before implementation that prove:

- a valid selector manifest digest produces a distinct asset version while
  `WEBUI_VERSION` remains unchanged;
- `WEBUI_ASSET_VERSION` equals the exact full lowercase digest;
- two manifest digests produce their exact distinct asset versions even for
  the same product version;
- malformed, uppercase, and missing values fail startup in selector mode;
- selector-only values in non-selector modes fall back to `WEBUI_VERSION`;
- the app shell, login page, and `/sw.js` substitute the asset version rather
  than the product version, with exact equality across every query token, the
  service-worker namespace, and its versioned shell-asset list; and
- existing Git/Docker version-detection tests remain green.

Focused verification uses the repository test runner and covers the version
resolver, app-shell template cache, static-asset resolver, service-worker
contract, and Windows Git fallback.

## Live deployment and acceptance

Build the immutable candidate from the exact currently running release commit,
not an older checkout. Preserve the running release as `last-good`.

Before cutover, require a continuous idle drain with no active runs or streams.
Record the expected digest from the selector-validated candidate manifest and
use that value, not a value reported by the candidate process, as the comparison
authority.

Poll startup identity and health once per second for at most 60 seconds. Give
each HTTP acceptance request a three-second connect/response deadline. After
selector activation, accept the candidate only when:

- `/health` reports the expected managed candidate and healthy admission;
- `/health.build.manifest_sha256` equals the recorded expected digest;
- the listener belongs to the new process;
- `/login` serves `static/login.js?v=<expected-digest>` exactly;
- `/sw.js` serves `hermes-shell-<expected-digest>` exactly and every resolved
  versioned shell-asset URL uses `?v=<expected-digest>`;
- neither response contains `unknown` or an unresolved version placeholder; and
- restart-boundary logs contain no new cache-version or startup failure.

If any startup, identity, or HTTP condition fails or exceeds its deadline,
atomically restore the preserved `last-good` release. Poll rollback
identity/health once per second for at most 60 seconds and apply the same
three-second deadline to each HTTP read-back. A rollback that misses its bounded
read-back remains a visible deployment failure and is never reported as
successful.

## Documentation

Update `ARCHITECTURE.md` to distinguish product version identity from the
browser asset-cache identity. Do not edit `CHANGELOG.md`; carry
release-note-ready wording in the deployment/PR receipt.
