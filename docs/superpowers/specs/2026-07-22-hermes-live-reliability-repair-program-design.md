# Hermes Live Reliability Repair Program Design

## Status

Proposed for written review. The user approved staged containment-first repair,
live deployment, and immediate deterministic acceptance. Long-running soak or
observation periods are explicitly out of scope.

## Goal

Repair the currently observed Hermes recovery, Codex transport, background
review, tool-contract, and WebUI latency failures; deploy each repair through
the real launchd/runtime owners; resolve the stranded recovery slot without
replaying its uncertain turn; and prove the live system immediately with
deterministic regression, fault-injection, canary, health, and log read-backs.

## Completion contract

The work is complete only when all of the following are true:

1. Every observed failure signature is assigned to a release and has a replay,
   regression test, live acceptance check, and rollback.
2. The exact bytes tested are the exact bytes installed.
3. WebUI, Hermes Agent, watchdog, cron configuration, and Codex CLI/runtime
   identities are recorded before and after deployment.
4. The stranded recovery claim is durably quarantined and the global slot is
   released without replaying the old turn or changing `state.db`.
5. A different eligible recovery can acquire and release the slot safely.
6. The two captured overlong-call-ID histories can continue without database
   rewriting.
7. Ambiguous Codex app-server turns cannot poison a later turn.
8. Background review cannot exceed its routed model's aggregate context budget
   or call tools it is not permitted to use.
9. Misconfigured or malformed tool calls fail once with an actionable typed
   correction rather than looping.
10. Live `/api/sessions`, model catalog, and chat admission meet the immediate
    performance gates below.
11. All scripted live acceptance checks pass after the final restart and log
    boundary. No multi-hour or multi-day soak is required.

## Non-goals

- Do not rewrite, migrate, truncate, vacuum, or delete canonical conversation
  history in `state.db`.
- Do not replay the stranded July 13 recovery turn.
- Do not merge or update unrelated upstream work while repairing these issues.
- Do not discard, overwrite, or silently absorb the user's current dirty
  WebUI or Agent changes.
- Do not start an otherwise unused external Orchestrero service merely to make
  the Hermes-internal orchestration repair look broader. Hermes delegation is
  in scope; external Orchestrero is in scope only if live configuration proves
  Hermes is supposed to depend on it.
- Do not add a long-running soak requirement after deterministic acceptance.

## Program decomposition

This is a program design, not one implementation plan. Each release below gets
its own implementation plan, worktree, tests, artifact manifest, cutover, and
rollback. Releases must not be combined merely to reduce restart count.

1. Release 0A: read-only deployment truth, dirty-state preservation, closed
   incident ledger, and impact-analysis repair.
2. Release 0B: dirty preview-query correction and immutable cutover substrate.
3. Release 1: recovery identity, status, fencing, quarantine, and watchdog
   reconciliation.
4. Release 2: hard aggregate background-review admission budget.
5. Release 3: paired legacy call-ID normalization.
6. Release 4: Codex app-server terminal and interrupt state machine.
7. Release 5: session-projection request-path repair.
8. Release 6: provider-catalog and chat-admission repair.
9. Release 7a: runtime capability manifest and planner/tool-schema parity.
10. Release 7b: typed tool errors and retry policy.
11. Release 7c: `web_extract` provider gating.
12. Release 7d: `search_files` regex/glob contract.
13. Release 7e: terminal timeout, cancellation, and orphan-process cleanup.
14. Release 7f: profile-qualified skill-mutation prerequisites.
15. Release 8: warning classification, operator-status accuracy, and Hermes
    delegation acceptance.

## Recovery-contract precedence

For this repair program, this document is authoritative where it supersedes
`2026-07-16-session-watchdog-stale-dispatch-recovery-design.md`. The older
document's remaining fail-closed constraints continue to apply. These clauses
are explicitly superseded:

- reuse of the general WebUI signing key is replaced by a dedicated recovery
  key and signed responses;
- bare `absent` is replaced by `absent_fenced`; absence without a durable fence
  can never release a slot;
- the old immediate repair's live `absent` requirement is replaced for the
  current superseded turn by the maintenance-window historical proof below;
- automatic or manual restoration of a watchdog-state backup is forbidden;
  backups are forensic only;
- `terminal_not_started`, one random `dispatch_id`, process-incarnation identity,
  and absorbing quarantine are added to the lifecycle contract; and
- forward deployment and rollback follow the versioned provider/consumer order
  defined in this document.

## State and source ownership

| Layer | Authority | Allowed repair behavior |
|---|---|---|
| `state.db` | Canonical transcript and logical-turn identity | Read-only |
| Turn journal | Durable WebUI recovery lifecycle | Append only through normal versioned lifecycle writes |
| `STREAMS` / `ACTIVE_RUNS` | Current-process live ownership hints | Read under the owning process lock; never sufficient alone to prove absence |
| Recovery ownership lease | Durable process/dispatch ownership | Versioned, fenced, and tied to one process incarnation |
| Watchdog state + lock | Machine-wide recovery coordination, authoritative fence, quarantine tombstone, and slot | Atomic identity-checked transitions only |
| Session sidecars | Runtime/recovery overlay | Existing application writes only |
| Provider/session caches | Rebuildable acceleration | May be invalidated or replaced; never canonical |
| Stored tool-call IDs | Canonical historical messages | Never rewritten; normalize only in outbound request copies |

## Release 0A: establish deployment truth

Release 0A is a read-only preflight. It creates evidence, manifests, the closed
incident ledger, and compatible worktrees, but installs nothing and is exempt
from the generic candidate cutover/canary procedure.

### Dirty-state preservation

Before the first restart:

1. Record repository URL, branch, HEAD, `git status`, tracked diff, untracked
   manifest, resolved symlinks, and hashes for both WebUI and installed Agent.
2. Record the currently running process command lines, launchd job definitions,
   listener ownership, interpreter/venv, Codex version, redacted config
   fingerprint, watchdog/config hashes, and state-file mode/owner.
3. Preserve user changes as explicit patch artifacts. Do not reset or stash them
   implicitly.
4. Build release worktrees from the exact live bases, then intentionally apply
   or exclude each preserved change. The resulting candidate must be a complete,
   clean, committed tree.
5. The candidate manifest records base commit, candidate commit/tree, applied
   patch hashes, complete changed-file list, test receipts, and artifact hashes.
6. Create and close the incident ledger before implementation begins. Every
   observed signature receives an owner, exact evidence window, deterministic
   replay or classification fixture, target release, telemetry field, immediate
   live check, and rollback. Each later release updates the ledger; Release 8
   performs final reconciliation rather than creating it.

## Release 0B: preview correctness and cutover substrate

### Current dirty preview query

The current `api/agent_sessions.py` dirty change calls
`_enrich_untitled_with_preview(projected, ...)` before applying `limit`. Its
claim that work is bounded to the visible page is false, and it reads/orders
message rows on a collection-projection request path. The running server
predates this edit, so it did not cause the already measured 205-second request,
but the first restart would load it.

The first WebUI candidate must preserve the intended untitled-title behavior
while restoring the projection contract: no unbounded message-table scan on the
request path. It must either bound rows before enrichment and prove indexed work,
or move preview materialization off the request path. This correction is a
restart prerequisite, not deferred cleanup.

### Immutable cutover

The live service must run a complete versioned candidate, not a mixture of files
copied into a dirty checkout. Existing launchd ownership remains authoritative;
an atomic versioned release selector outside the candidate records `current`,
`candidate`, and `last-good`, exposes the selected build ID, and can return to
`last-good` if the candidate fails bounded startup health. The selector is not a
new daemon: launchd invokes it and it immediately execs the selected release.

Before cutover, poll once per second and require `active_runs=0` and
`active_streams=0` continuously for 30 seconds. If either becomes nonzero, reset
the drain timer; if no drain is achieved within the declared maintenance window,
abort rather than interrupt work. After cutover, verify launchd PID, listener
owner, process command, build ID, and `/health`.

Release 0B bootstraps this selector exactly once:

1. preserve the current launchd definition, checkout evidence, hashes, and
   restart command for forensics, but do not use the dirty checkout itself as a
   restart target;
2. build an immutable `pre-selector-last-good` snapshot from the exact base plus
   only intentionally retained user changes. Exclude or correct the unbounded
   preview query and test the complete snapshot before it may serve rollback;
3. build the selector and first complete candidate offline;
4. prove the selector, `pre-selector-last-good`, and candidate with an isolated
   launchd label, port, state directory, and copied/synthetic state;
5. complete the defined live drain and back up the launchd definition;
6. atomically replace only the launchd program path with the selector path and
   restart;
7. if the 60-second identity/health protocol fails, point the preserved launchd
   definition at the immutable tested `pre-selector-last-good` snapshot and
   restart it; and
8. after success, all later releases use selector-managed `candidate`, `current`,
   and `last-good` paths.

No recovery state schema has changed during this bootstrap, so restoring the
tested `pre-selector-last-good` launchd target is safe and independently
reversible. The dirty checkout is never restarted as rollback.

### Code intelligence gate

GitNexus currently cannot perform required impact/context calls because the
index database and reader versions differ. Re-index the exact release worktree,
record its indexed commit/path, and prove `context` and `impact` work before
editing any shared symbol. Run `detect_changes` against the explicit base before
each commit.

## Release 1: recovery ownership and reconciliation

This release extends the existing stale-dispatch recovery design rather than
replacing its fail-closed invariants.

### One attempt identity

Generate one cryptographically random `dispatch_id` per recovery attempt. Carry
it unchanged through the watchdog slot and claim, signed start/status request,
submitted/worker/terminal journal events, live registries, ownership lease, and
status response. Matching is conjunctive across dispatch ID, profile, logical
turn, fingerprint, and server instance; never token-or-fingerprint.

The lifecycle identity is independent of mutable physical session ID. A
compression successor retains the original dispatch/turn identity so status can
follow the canonical lineage without misclassifying a rotated session as absent.

### Process identity and protocol

Each WebUI start creates a durable `server_instance_id` associated with PID start
identity and host boot identity. The status protocol is versioned and advertises
provider build/capability IDs. A process may report `absent` only for a
reservation it owned or after authoritative proof that the recorded process
incarnation cannot still run.

Use a dedicated recovery signing key. Bind responses to request nonce, path,
dispatch ID, reservation, server instance, status, and expiry. Unsigned,
unsupported, stale, or mismatched responses become `unknown`.

### Durable fence and quarantine

A read-only absence response cannot by itself release a slot. Before release,
the watchdog uses a two-phase transition in the watchdog state file, which is
the authoritative fence/quarantine/slot store:

1. Under the watchdog state lock, exact-compare claim revision, slot,
   `dispatch_id`, process incarnation, turn, profile, and fingerprint, then
   atomically persist `fence_requested` while retaining the global slot. Existing
   per-dispatch side-effect leases remain recorded; new leases are denied.
2. Every worker acquires a unique durable `side_effect_lease` under the same
   watchdog state lock immediately before each provider request or tool/process
   side effect. Lease acquisition and fence installation are therefore ordered
   by one lock: if the lease wins, the watchdog sees it and cannot drain; if the
   fence wins, the worker cannot begin the effect. The worker releases its lease
   under the state lock only after the effect and every spawned process in its
   process group have terminated. A crash leaves the lease durable until the
   recorded process incarnation is proven dead and no process-group member
   survives.
3. Release the watchdog lock. WebUI launch and recovery-resume paths also check
   the fence before worker registration. A matching fence blocks registration
   and any new side-effect lease.
4. The watchdog queries the versioned status provider without holding its state
   lock. Only after status proves the owner non-live, no matching process group
   exists, and the authoritative state contains zero side-effect leases does the
   watchdog atomically advance the fence to `fence_drained`.
5. With `fence_drained` durable, reacquire the state lock, exact-compare the same
   revision/identity, and
   atomically persist the quarantine tombstone, audit transition, and slot
   clear in one state-file replacement.

No path holds the watchdog state lock while making HTTP calls. The lock order is
WebUI session lock, then a short watchdog-state read; there is no path that holds
the watchdog lock and waits for the WebUI session lock. A crash before phase 1
leaves the original dispatch untouched; a crash after phase 1 leaves a retained,
restartable fence; a crash during finalization converges to either the previous
fenced state or the fully quarantined-and-cleared atomic replacement.

`fence_drained` is written only by the watchdog under the state lock; there is
no informal worker acknowledgement. A delayed worker that resumes after fencing
cannot acquire a side-effect lease and must abort.

`manual` is an absorbing, durable no-replay state for the logical turn and
dispatch. Only a separate audited operator action may remove its quarantine.
Unknown terminal outcome with proven fenced/dead ownership may release the
global slot while retaining the per-session quarantine. Unknown ownership
retains the slot and emits a non-silent blocker.

The state transition that records quarantine, audit data, claim revision, and
slot clear is one crash-consistent atomic write under the state lock. External
logs are post-commit projections, never the authoritative transition.

An older WebUI that does not advertise the fence protocol cannot acknowledge a
fence. An older watchdog that cannot parse the new state schema may not be
re-enabled after any fence or quarantine exists. These version mismatches fail
closed and require the compatible provider/consumer pair or an explicit manual
recovery procedure; they never downgrade state.

### Status states

The versioned status response supports:

- `live`
- `terminal_recovered`
- `terminal_blocked`
- `terminal_uncertain`
- `terminal_not_started`
- `absent_fenced`
- `unknown`

Live ownership always wins over terminal-looking evidence. Malformed, duplicated,
reordered, cross-dispatch, cross-instance, or contradictory evidence is unknown.

### Timeout contract

Validate at runtime that the cron script timeout exceeds the recovery timeout,
reconciliation grace, status/finalization budget, and runner termination margin.
Install watchdog and timeout configuration as one unit. A partial or invalid
combination must refuse dispatch with a visible nonzero health result.

### Existing stranded slot

The existing slot references an older user turn than the canonical session and
cannot automatically return `absent`. Resolve it only after the new provider and
consumer are installed and verified:

1. disable new watchdog dispatch and wait for any invocation to exit;
2. drain WebUI activity;
3. stop WebUI and prove its PID/listener and recovery workers are gone;
4. acquire the watchdog state lock and re-read the exact slot/claim revision;
5. prove the old historical turn still exists, the later successor is
   unambiguous, and the exact dispatch has no live process incarnation;
6. create a permission-preserving state backup;
7. atomically mark the old claim quarantined/manual with reason
   `superseded_turn_after_abandoned_dispatch` and clear only the matching global
   slot;
8. never replay the old turn or restore its state backup automatically;
9. restart WebUI, verify health/build ownership, then re-enable the watchdog.

### Immediate acceptance

In isolated state, inject crashes after reservation, after worker registration,
and during finalization. Prove no duplicate recovery, no replay after quarantine,
and convergence after every atomic-write boundary. Run two WebUI processes
against shared test state and prove a non-owner cannot return releasable absence.
Force compression during recovery and prove lifecycle identity survives.

Live acceptance manually invokes successive watchdog ticks rather than waiting
for a soak. It creates a dedicated tagged canary session through normal Hermes
APIs, with a harmless no-side-effect prompt and a controlled synthetic
interruption; it never selects an existing user conversation or the stranded
turn. The old claim remains quarantined, the slot is free, the canary acquires
and terminates, and a final tick observes no stale slot or silent-green blocker.

The repair implementation never writes canonical conversation rows directly.
Dedicated canary sessions may create ordinary new canonical rows through the
normal Hermes API; they are tagged and archived after proof, not deleted. Exact
legacy poisoned histories are replayed only against an isolated copy of state.

### Recovery forward and rollback compatibility

Forward deployment order is fixed:

1. deploy WebUI provider with protocol/build capability, keeping the old
   watchdog paused;
2. verify provider-new/consumer-old is inert because no old invocation remains;
3. install watchdog plus timeout configuration as one compatible unit;
4. prove the running scheduler loaded the expected consumer protocol and timeout
   inequality;
5. resolve the existing slot; and
6. enable dispatch and run the tagged canary.

Consumer-new/provider-old, unsupported schema, malformed status, and key mismatch
all refuse dispatch visibly. Rollback disables and drains the consumer first,
then restores a consumer/config pair that understands every state schema already
written, and only then rolls back the provider. Once a fence or quarantine has
been written, rolling back to software that cannot parse it is forbidden.

The first write of recovery schema v2 (`dispatch_id`, side-effect lease, fence,
or quarantine) is the irreversible schema-commit point. Immediately before
permitting that write, promote a tested minimal provider/consumer pair that
parses v2 and keeps dispatch disabled on uncertainty as the compatibility-floor
`last-good`. Before the schema-commit point, automatic rollback may select the
pre-v2 build. After it, automatic rollback may select only a v2-compatible
build; the pre-v2 artifact remains forensic/manual and is never auto-selected.

## Release 2: background-review admission fuse

Before constructing a routed review request, compute an aggregate admission
budget covering system prompt, tool schemas, retained messages, tool results,
cached-input accounting, child/review context, requested output, and a synthesis
reserve. The request must fit the target model's actual context window. Oversized
tool output is deterministically digested or omitted; if the request still does
not fit, review skips with one typed result and does not touch parent-session
compression.

Immediate acceptance replays the 669,788-versus-262,144 incident and proves the
request is bounded or skipped before provider submission.

## Release 3: paired call-ID normalization

Normalize only the outbound Responses request copy. The exact maximum is 64
UTF-8 bytes. Preserve a nonempty original ID byte-for-byte when its UTF-8
encoding is at most 64 bytes. For an invalid or oversized legacy ID, hash the
original UTF-8 bytes with SHA-256 and emit `call_h_` plus the first 56 lowercase
hex characters, producing 63 ASCII bytes. Apply one request-scoped mapping to
every matching `function_call` and `function_call_output`; distinguish `call_id`
from response-item IDs and fail closed on an ambiguous duplicate source ID or a
mapping collision.

The projector also generates bounded new IDs so new history is clean. Canonical
history is never migrated or rewritten. The mapping is deterministic and stable
across retries, compression, replay, resume, and repeated construction of the
same outbound request.

Acceptance replays both captured failing lineages, tests boundary and Unicode
inputs plus generated collision/pairing cases, and completes a live MCP tool call
and follow-up from previously poisoned history without a 400.

## Release 4: app-server terminal state machine

A completed-looking assistant message without correlated `turn/completed` is a
`terminal_ambiguous` candidate, not proof of success. Salvaged text may be shown
as partial output but must not trigger success-only memory, review, or curation
hooks. The Codex client is retired and cannot serve the next turn.

Track root turn/thread identity, outstanding tools, approvals, RPCs, and process
EOF. Late, child, stale, duplicated, or reordered events cannot complete the
root turn. Interrupt timeout, EOF, malformed response, missing correlated
terminal, and declined/rejected tool silence all reach bounded explicit outcomes
and retire when ownership is ambiguous. Pending RPC waiters wake immediately on
process exit.

Use a five-second terminal-candidate grace only after a completed root
`agentMessage` and only while the root turn has zero outstanding tools,
approvals, or RPCs. Any correlated root activity cancels/restarts the candidate;
child or stale activity cannot affect it. Expiry returns partial text as
`terminal_ambiguous`, triggers no success-only hooks, interrupts/retires the
client, and waits for bounded process teardown. A later event can never convert
an already returned ambiguous outcome into success.

Acceptance replays all captured missing-terminal/retirement/interrupt sequences,
then runs live no-tool, successful-tool, failed-tool, rejected-approval,
interrupt, and follow-up turns. No ambiguous client may be reused and no message
may be duplicated.

## Releases 5 and 6: WebUI request paths

### Session projection

Profile the exact cache-lookup/rebuild stages on a read-only production-shaped
copy. The request thread may read bounded index/session metadata and a cached
snapshot, but must not scan message history or wait behind the full background
projection. Preserve cold/warm payload parity, canonical lineage, profile scope,
and runtime overlays.

Measurement starts immediately before the localhost HTTP request is written and
ends after the complete response body is read; it includes authentication,
routing, serialization, and response transfer, but not DNS. "Cold" means a new
isolated WebUI process with empty in-process caches against the unchanged copied
database; the OS page cache is not claimed to be purged. Run 20 cold processes,
100 warm sequential requests after one unmeasured prime, and 100 concurrent
requests as ten workers making ten requests each.

Immediate gates on the copied 4.27 GB shape:

- cold `/api/sessions` p95 below 2 seconds;
- warm p95 below 250 ms;
- maximum below 5 seconds under ten concurrent readers;
- no request-thread message-table scan;
- exact cold/warm response parity after canonical JSON key ordering. No response
  field or value may be discarded or normalized.

After live deployment, run 20 sequential requests and five simultaneous requests
after one unmeasured prime. Require the same warm-path shape plus no cache-lock
stage above 250 ms.

### Provider catalog and chat admission

`prefer_cache` and last-known-good reads must return before waiting behind a live
provider rebuild. Refresh remains single-flight and out-of-band. Expose fallback
reason, freshness age, selected source, and routability without secrets.

Distinguish catalog latency, chat-admission latency, and model first-token
latency. Catalog measurement uses 100 cached calls and 20 cold-last-known-good
calls in isolated state. Chat-admission measurement runs 100 isolated disposable
requests with immediate cancellation after `stream_id`; it measures request
write through admission response and excludes first-token time. Immediate gates:

- cached catalog p95 below 250 ms;
- cold last-known-good response below 1 second while refresh continues;
- chat admission p95 below 2 seconds and p99 below 5 seconds across the 100
  isolated requests;
- the selected model/provider is routable rather than merely present in stale
  display data.

## Releases 7a-7f: tool contracts

### Release 7a: capability manifest

The planner-visible tool schema is derived from the active profile's actual
runtime capability manifest. Routed background review exposes only its allowed
tools. Configuration/profile changes invalidate the manifest. Runtime denial is
defense in depth, not the primary advertisement mechanism. Acceptance switches
profiles/configuration and proves advertised schemas change atomically while a
routed review exposes only permitted tools. Rollback restores the previous
manifest builder without changing persisted configuration.

### Release 7b: typed errors and retry policy

Classify errors as permanent permission/auth/capability, schema-correctable,
transient provider/runtime, timeout/cancelled, or internal. Permanent errors do
not retry. Schema-correctable errors receive at most one corrected retry. Loop
guarding groups equivalent normalized failures rather than exact argument text.
Acceptance replays each captured permanent and correctable class, proving zero
permanent retries and at most one corrected retry. Rollback may restore the old
presentation/classifier only for future calls, but retains a conservative safety
floor: unknown classes receive no automatic retry, permanent classes receive
zero, and schema-correctable classes receive at most one. Already failed calls
are never replayed. A rollback may not restore the former unbounded retry
behavior.

### Release 7c: `web_extract` routing

`web_extract` is advertised only when the active profile has an extraction-
capable provider. DDGS search alone never enables it. Acceptance covers DDGS-
only absence, configured extractor success, profile switching, credential loss,
and runtime configuration invalidation. Rollback restores only extraction
registration/routing.

### Release 7d: `search_files` regex/glob contract

Obvious glob-as-content-regex input fails fast with a typed instruction to use
file-target mode; it is not silently reinterpreted. Acceptance replays every
captured malformed pattern and proves one correction maximum, valid regex
parity, explicit file-glob behavior, and safe `--` argument termination.
Rollback restores only argument validation/error shaping.

### Release 7e: terminal timeout and process cleanup

A broad terminal timeout cancels the complete process group and proves no orphan
remains. Repeating the same broad scope requires a narrower root. Acceptance
starts controlled child/grandchild processes, forces timeout/cancellation, and
proves every PID exits while unrelated processes survive. Rollback changes only
the timeout/cancellation controller.

### Release 7f: skill-mutation prerequisites

Skill mutation requires a viewed, profile-qualified target and stops after a
failed prerequisite instead of looping. Acceptance replays all captured
`skill_manage` failures, including missing view, wrong profile, ambiguous
replacement, stale source, invalid frontmatter, and blank target. Rollback
restores only skill-mutation admission/error shaping.

## Release 8: observability and orchestration acceptance

Reconcile and close the Release 0 incident ledger after all owned releases;
do not create it here. Expected
registry gates move out of warning severity. Bundled-skill symlink warnings are
resolved through verified install identity rather than blanket symlink trust.
OpenViking structured batches are bounded or chunked before the 100-message
limit. Health probes use the current authenticated gateway contract. `ctl.sh`
must not report stopped when launchd owns a healthy listener.

Hermes-internal delegation acceptance runs a real create/execute/observe/complete
child task and proves terminal delivery. If configuration shows an intended
external Orchestrero dependency, that service gets a separate precondition and
canary; otherwise its stopped state is recorded as out of scope rather than
misreported as a Hermes failure.

## Cutover and rollback rules

Release 0A is non-deploying, and Release 0B uses its one-time bootstrap procedure
above. For every deployable release after 0B:

1. build and test an immutable candidate;
2. run isolated acceptance on copied/synthetic state;
3. capture pre-cutover hashes and health;
4. perform the one-second polling, 30-continuous-second zero-activity drain
   defined in Release 0B and abort if the maintenance window expires;
5. select the candidate atomically and restart through launchd;
6. poll once per second for at most 60 seconds and require a new PID, expected
   listener owner, candidate process path/build ID, healthy endpoint, and exact
   installed hashes; otherwise atomically select `last-good` and restart;
7. execute that release's dedicated tagged live canary immediately; never use an
   existing user conversation as a test target;
8. read logs from the restart boundary and compare against its incident ledger;
9. mark candidate `last-good` only after those checks pass.

Rollback runs consumers before providers. For recovery, disable/quiesce the
watchdog, restore watchdog and timeout configuration together, then remove the
status provider only after no compatible consumer or reservation remains.
Watchdog state backups are forensic and are never restored automatically.

If startup health or the immediate live canary fails, the external launcher
selector selects `last-good`, restarts, and repeats the same 60-second identity
and health protocol against the rollback build. No canonical
state rollback is part of any release.

## Final immediate acceptance receipt

The final receipt contains:

- source/base/candidate/installed hashes for every component;
- preserved dirty-change patch hashes;
- GitNexus impact and change-detection results;
- focused and broader regression commands with outputs;
- launchd PID/listener/process/build read-backs;
- recovery quarantine and slot-transition audit without prompt content;
- proof the old turn was never replayed and a different safe recovery completed;
- exact captured call-ID and app-server replay results;
- background-review and tool-contract replay results;
- copied-database and live request-path timing summaries;
- Hermes delegation canary result;
- zero occurrences of every ledgered failure signature in logs produced by the
  scripted post-deploy acceptance run;
- the exact rollback target and verified rollback command.

Once this receipt passes, the repair is done. There is no additional soak gate.
