# Managed Startup Manifest Identity Separation

**Status:** Approved for implementation
**Date:** 2026-07-31

## Problem

Managed release startup currently gives `HERMES_WEBUI_MANIFEST_SHA256` two
incompatible meanings:

- the selector and direct-fallback launchers set it to the immutable WebUI
  package manifest hash; and
- managed startup replay compares it with the canonical deferred-startup
  operation manifest hash.

Those hashes describe different artifacts and normally differ. A
startup-fenced candidate therefore passes immutable package verification, then
fails closed when signed acceptance runs deferred startup. The candidate remains
fenced and cannot be promoted.

The package hash is already part of the managed build identity and must not be
redefined or weakened.

## Decision

Keep `HERMES_WEBUI_MANIFEST_SHA256` exclusively as the immutable package
manifest identity. Add
`HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256` exclusively for the canonical
deferred-startup operation manifest.

The selected immutable release's `bootstrap.py` is the producer of the new
value. After it has validated the existing managed package environment, it
computes the deferred manifest hash from the selected release's sealed
`deferred_release_manifest.py` and binds that hash into the environment inherited
by `server.py`.

This producer location covers both selector launches and direct-fallback
launches without teaching the externally installed selector how to import code
from a release that it has not started.

## Alternatives Rejected

1. Remove the startup manifest comparison. This would allow acceptance without
   proving the replay operation set and order.
2. Redefine `HERMES_WEBUI_MANIFEST_SHA256` as the deferred manifest hash and add
   another package key. This would break the existing build identity contract,
   selector verification, health attestation, and release receipts.
3. Hard-code the deferred manifest hash into the installed selector. This would
   couple selector installation to every future startup-manifest change and
   make old selector binaries unable to launch a valid new release.

## Data Flow and Ownership

1. The selector or direct-fallback plist supplies
   `HERMES_WEBUI_MANIFEST_SHA256` from the verified immutable release record.
2. `bootstrap.py` validates the complete managed package environment and proves
   that its own code root is the selected immutable release.
3. For a startup-fenced launch, `bootstrap.py` computes the canonical deferred
   manifest hash from that same release. It refuses a conflicting pre-supplied
   value and otherwise exports
   `HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256` before replacing itself with
   `server.py`.
4. `managed_startup_coordinator.py` and the server's managed session-recovery
   binding compare only the dedicated deferred-manifest key with the canonical
   deferred manifest hash.
5. Build identity and health attestation continue to verify and report the
   package manifest through `HERMES_WEBUI_MANIFEST_SHA256` unchanged.

The immutable release owns both truths: its package manifest authenticates the
files, while its deferred manifest authenticates startup replay semantics.

## Failure Behavior

- A startup-fenced managed launch with a missing, malformed, or conflicting
  deferred-manifest binding fails before any deferred startup mutation.
- An unfenced managed launch does not require the deferred key because it does
  not execute managed deferred replay.
- Legacy unmanaged bootstrap remains unchanged.
- The signed release-control accept path continues to fail closed on any
  identity mismatch.

## Test Design

The regression test must first fail against the current code by using two
different valid SHA-256 values: one package manifest hash and the real canonical
deferred manifest hash.

Coverage will prove:

- managed bootstrap preserves the package hash and exports the distinct
  deferred hash;
- a conflicting pre-supplied deferred hash is rejected;
- startup coordinator construction accepts the two correctly separated hashes
  and rejects a missing or incorrect deferred binding;
- managed session recovery uses the dedicated deferred binding;
- selector and direct-fallback tests continue to assert the original package
  manifest environment unchanged.

Focused tests will run through `./scripts/test.sh`; neighboring managed-startup,
release-fence, bootstrap, and selector tests will run before release packaging.

## Release and Recovery

The fix must ship in a fresh immutable candidate; the already sealed r97
artifact will not be modified or promoted. Release execution retains the merged
bounded force-restart behavior for launchd teardown races and must verify:

- signed startup-fenced health before acceptance;
- successful deferred startup acceptance and open admission;
- exact WebUI/Gateway paired identity after promotion;
- selector `candidate` and `pending_transaction_id` cleared;
- zero durable completion backlog; and
- rollback viability from sealed receipts.
