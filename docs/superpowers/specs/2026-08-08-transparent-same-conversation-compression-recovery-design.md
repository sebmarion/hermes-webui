# Transparent Same-Conversation Compression Recovery Design

**Date:** 2026-08-08

**Status:** Approved direction; implementation pending

**Contract family:** runtime, streaming, recovery, transcript, and session identity

## Decision

When a turn reaches structured `compression_exhausted`, Hermes WebUI will
recover automatically in the same logical conversation. Recovery must not
create a focused-continuation session, change the title, navigate to another
URL, add a sidebar row, require a button click, or make the conversation
read-only.

The server will durably capture one safe, bounded recovery seed before the
failed worker releases ownership. After that worker unregisters, it will start
one hidden continuation turn against the same current session. The browser is
an observer of that server-owned turn, not the owner of recovery.

The full visible transcript remains intact. Only the model-facing
`context_messages` are replaced with the bounded recovery seed.

## User-visible outcome

The required invariant is:

> Context exhaustion may interrupt one worker, but it must not move the user to
> a different conversation or require the user to reconstruct and resubmit the
> task.

During normal recovery, the current terminal error is replaced by the existing
quiet compression-status treatment: `Recovering context...` while the parent
settles, followed by the successor's streamed answer in the same conversation.
The recovery status is transient and does not become a permanent card in the
settled transcript.

Only a recovery that cannot be performed safely becomes visible as an explicit,
retryable blocker. Even then, the composer remains available in the same
conversation; the UI never instructs the user to open a new task.

## Incident and root cause

The current implementation converts `compression_exhausted` into a terminal
read-only state with a recommended `start_focused_continuation` action. The
action constructs a new `Session`, clears its visible messages, gives it a
`(focused continuation)` title, and waits for the browser to navigate to it.
Its model seed contains only the newest compressed summary when one exists; a
generic authorization such as "do it" can therefore lose the assistant's
immediately preceding plan when no summary was persisted.

This protects the exhausted session from an oversized replay, but exposes an
internal context-management boundary as a broken conversation boundary. The
manual browser step is also the wrong durability owner: a closed or refreshed
tab cannot complete it.

The repository already has the pieces needed for a smaller repair:

- separate visible `messages` and model-facing `context_messages`;
- turn-journal rows that durably identify the submitted user turn;
- per-session locks and execution-lineage admission;
- server-owned same-session goal continuations;
- durable continuation receipt and startup-recovery primitives;
- a post-`unregister_active_run()` successor hook; and
- a persistent per-session live-view channel with `server_turn_started`.

## Goals

- Keep the same logical conversation and current canonical session tip.
- Preserve the full visible transcript exactly once.
- Preserve the exact failed user request and its surviving attachments.
- Recover useful task state when no compressed summary exists.
- Start recovery without an open browser and resume live streaming when a tab
  is open.
- Make duplicate callbacks, reloads, multiple tabs, and safe restart replay
  idempotent.
- Fail closed when the recovery seed or launch history is ambiguous, without
  making the conversation read-only.
- Apply the behavior through the shared native/Gateway start path rather than
  a browser-only branch.

## Non-goals

- No new user-visible task, fork, title, URL, or sidebar model.
- No background summarization service, chunked summarizer, checkpoint daemon,
  scheduler, database table, user setting, or configurable retry policy.
- No replay of raw tool output, reasoning traces, synthetic controls, provider
  errors, or unbounded transcript history into the fresh model context.
- No rewrite or deletion of the visible historical transcript.
- No automatic repetition after a recovery continuation itself exhausts
  context; that is treated as no progress and disclosed in place.
- No change to ordinary successful automatic-compression lineage. If the Agent
  already rotated to a canonical compression tip during the failed run,
  recovery uses that tip; it does not create an additional recovery session.
- No migration or merging of focused-continuation sessions that already contain
  substantive user or assistant work.

## Terms

| Term | Meaning |
| --- | --- |
| Current session | The canonical session tip owned by the failed worker when terminal writeback commits. |
| Visible transcript | Durable user/assistant rows rendered as the conversation. |
| Model context | The bounded `context_messages` supplied to the next provider turn. |
| Recovery seed | Sanitized context rows plus the exact failed user turn and safe attachment references. |
| Recovery claim | A durable idempotency receipt keyed by current session ID and parent stream ID. |
| Recovery continuation | One server-started hidden-control turn with source `compression_recovery`. |
| Human supersession | A newer human chat start that consumes the seed and replaces the pending automatic control prompt. |

## Required invariants

1. Compression recovery never constructs a new `Session` or changes session
   identity, title, project, archive state, pin state, or sidebar membership.
2. The visible failed user row remains present exactly once, with its original
   text, source, timestamp, and attachment metadata.
3. Replacing `context_messages` never truncates, rewrites, or reorders visible
   `messages`.
4. The exact failed user request crosses the recovery boundary once as a
   user-role seed row; the hidden control does not copy it again.
5. Recovery context contains no raw tool results, tool arguments, reasoning,
   synthetic controls, terminal errors, or secrets copied from assistant
   material without redaction.
6. A durable recovery claim exists before the parent turn reports automatic
   recovery as accepted.
7. The successor starts only after the parent releases `STREAMS`,
   `ACTIVE_RUNS`, and execution-lineage admission.
8. At most one successor can be admitted for a recovery claim. An ambiguous
   post-launch crash fails closed instead of repeating possible tool effects.
9. A newer human turn supersedes a still-pending automatic control; the old
   request is never launched later as stale work.
10. An open tab follows the server-started stream, while a closed tab has no
    effect on whether recovery runs.
11. A recovery continuation that reaches `compression_exhausted` cannot create
    another automatic recovery continuation.
12. Every unsafe or unavailable path leaves the same conversation writable and
    reports truthful recovery state.

## Architecture

### 1. Recovery seed builder

`api/compression_recovery.py` remains the single policy owner for deciding what
may enter the fresh model context. The focused-fork builder is replaced by a
same-session seed builder with an explicit input boundary: current session,
parent stream ID, and the active turn identity captured before pending state is
cleared.

The builder emits bounded context rows and receipt metadata. It does not save a
session or start a worker.

Seed priority is:

1. the newest non-empty trusted compressed summary, if present;
2. otherwise, the latest substantive assistant checkpoint before the failed
   user turn;
3. optionally, bounded non-error assistant partial text produced by the failed
   turn, labeled as partial and unverified; and
4. the exact failed user request with its safe surviving attachment metadata.

A summary and checkpoint are alternatives, not cumulative transcript replay.
The seed has one total character budget using the existing bounded-text policy.
Assistant-derived text is redacted with the existing redactor before it crosses
into the fresh context. The original user request is preserved exactly and is
not logged. Tool calls, tool outputs, reasoning, `_error` rows, recovery rows,
synthetic controls, and context-management markers are excluded.

The assistant reference tells the model that it is recovery context, that
partial work is unverified, and that it must inspect the current workspace and
existing results before repeating actions. The final seed row uses role `user`
and carries the original request. The later hidden control says only to resume
the unfinished request in the recovery context, so the request is not duplicated.

The seed is trustworthy when at least one of these is true:

- a trusted summary exists;
- a substantive assistant checkpoint exists; or
- the failed user request is independently substantive.

A generic/deictic request with no summary or checkpoint is insufficient. That
case becomes an in-place blocker rather than invented context.

Attachment entries come from the active turn authority or its exact submitted
turn-journal row, not a reverse text scan. They retain only the metadata already
accepted by the chat-start boundary. Required local files are revalidated at
use; missing or conflicting attachments block recovery rather than silently
dropping them.

### 2. Durable recovery claim

Compression recovery adds one bounded receipt store beside the existing goal
and tool-limit continuation stores. It reuses their atomic replace, private
lock, stable-store validation, process-owner token, and managed startup
recovery primitives. It does not introduce a scheduler or database table.

The claim key is a digest of `(session_id, parent_run_id)`. A receipt contains:

- claim key, session ID, parent run ID, normalized profile, and source;
- a bounded recovery seed and safe attachment descriptors;
- the exact submitted turn ID and a transcript/seed fingerprint;
- state `claimed`, `starting`, `started`, or `discarded`;
- process/start ownership fields used by the existing receipt pattern; and
- a discard or blocker reason when recovery cannot proceed.

The terminal handler builds and saves the seed after materializing the exact
user turn and partial assistant output, but before clearing runtime ownership.
Session metadata records only the current recovery phase and claim identity;
the receipt owns the pending start.

Unreadable, oversized, schema-invalid, or identity-conflicting receipt state
fails closed. It never falls back to raw transcript replay.

### 3. Same-session continuation start

After `unregister_active_run(parent_run_id)`, the existing successor-recovery
hook gives compression recovery first opportunity for its exact parent claim,
before generic deferred wakeups. It reserves the receipt, atomically installs
the seed as the same session's `context_messages`, attaches structured
`compression_recovery` control metadata, and calls the shared `_start_run()`
path with:

- the same current session object and ID;
- source `compression_recovery`;
- the existing workspace, model, provider, profile, and execution lineage;
- the hidden continuation control prompt; and
- the claim token and seed fingerprint for turn-journal provenance.

`compression_recovery` joins `goal_continuation` and
`tool_limit_continuation` as an internal-control source. Its control prompt is
removed from visible display settlement and title generation, while one
structured marker may remain in model context for crash reconciliation.

The start path must use the same attachment validation and native/Gateway
routing as a human turn. It must not call the old focused-continuation endpoint
or depend on browser JavaScript.

### 4. Startup and crash reconciliation

Startup scans bounded recovery receipts through the existing managed
continuation recovery boundary.

- `claimed` with no submitted successor turn is safe to start.
- `starting` owned by the current live process is left alone.
- `starting` with a dead owner and no submitted successor, or an exact durable
  `launch_failed` event, is safe to reclaim.
- An exact submitted successor with a matching live/completed terminal record
  is reconciled as started.
- An exact submitted successor with no conclusive launch/terminal record is
  ambiguous. It is discarded with an in-place blocker; possible tool effects
  are not repeated automatically.

This is at-most-once launch across known evidence and fail-closed across the
unprovable crash window. The user can inspect and retry from the same
conversation if ambiguity is disclosed.

### 5. Human supersession

A human `/api/chat/start` and an automatic recovery start serialize on the same
session and lineage locks.

If a human start wins while a claim is still `claimed`, the server atomically:

1. verifies the claim still matches the current transcript boundary;
2. installs the recovery seed as `context_messages`;
3. marks the automatic claim discarded as `superseded_by_user`; and
4. starts the human's new message normally.

The new human message is the current intent. The old automatic control is never
launched afterward. If the automatic recovery stream already owns the session,
the existing active-stream behavior applies; the claim is not duplicated.

The terminal `compression_exhausted` marker therefore no longer blocks either
generic or substantive human messages. A human message is also the recovery
path when automatic seed persistence previously failed.

### 6. UI behavior

The manual focused-continuation card, `Start focused continuation` action, and
read-only composer gate are removed for compression exhaustion.

The current stream may still close with a structured terminal frame, but when
that frame includes a durable automatic-recovery claim the frontend renders it
as transient `Recovering context...`, not as a red terminal card. The existing
per-session channel receives `server_turn_started` for the successor, attaches
the normal stream renderer, and keeps the same selected conversation.

On reload, pending/running recovery state is derived from session metadata and
active-run truth. On successful successor settlement the metadata is cleared,
so settled history contains no recovery card or synthetic user row. A blocked
state renders one concise diagnostic with an in-place retry affordance or an
instruction to send a clarifying message; it never proposes a new task.

The old `/api/session/compression-recovery/start` route and its new-session
creation path are removed from the active UI contract. A cached client request
may be rejected with a typed response that tells it to reload; it must not
create a session.

## Data flow

1. A native or Gateway-backed turn returns structured
   `compression_exhausted`.
2. The streaming owner settles display/model arrays using the existing
   compression-exhausted boundary, materializes the exact user row, and
   snapshots any safe partial assistant text.
3. Under the session lock, the seed builder uses the active turn identity and
   exact submitted journal event to construct and fingerprint a bounded seed.
4. The server durably writes the recovery claim and session phase. Only then
   does the terminal frame state that recovery is pending.
5. The parent clears pending fields, saves the visible transcript, closes its
   stream, and unregisters its active run.
6. The post-unregister hook reserves the exact claim, installs model context,
   and starts the hidden same-session turn.
7. An open browser follows `server_turn_started`; a closed browser is irrelevant.
8. The recovery worker emits normal assistant/tool streaming and appends its
   final assistant answer to the existing visible transcript.
9. Successful terminal writeback clears session recovery metadata. A repeated
   `compression_exhausted` from source `compression_recovery` records a blocker
   and starts no successor.

## State ownership

| State | Owner | Release/settlement rule |
| --- | --- | --- |
| Visible transcript | Existing session/state reconciliation | Never replaced by recovery; next answer appends normally. |
| Model context seed | Current session `context_messages` | Installed only after claim reservation; normal result writeback supersedes it. |
| Failed user turn | Active turn identity plus turn journal | Materialized once before pending fields clear. |
| Pending recovery | Durable recovery receipt | Moves through claim/start or a terminal discard reason. |
| Runtime execution | Existing stream, active-run, and lineage admission | Parent releases before successor admission. |
| Live recovery presentation | Session recovery phase plus per-session channel | Cleared on successful successor settlement. |
| Focused-fork sessions | Existing historical data only | Never created, merged, or deleted by this change. |

## Error handling

| Failure | Required behavior |
| --- | --- |
| No trustworthy seed | Save an in-place blocker; keep composer enabled; do not start or fork. |
| Receipt write fails | Report that automatic recovery could not be persisted; keep same conversation writable. |
| Attachment no longer resolves | Name the missing attachment safely and block; never omit it silently. |
| Parent still active | Keep receipt claimed and retry only at the next verified idle boundary. |
| Competing human turn | Human supersedes the claimed automatic control and consumes the seed. |
| Exact recovery stream already live | Reconcile the receipt to that stream; do not start another. |
| Startup proves no launch | Reclaim and start once. |
| Startup launch evidence is ambiguous | Fail closed with an in-place blocker; do not repeat possible effects. |
| Recovery worker exhausts context | Mark no progress, clear pending recovery, show blocker, and start no loop. |
| Provider/auth/model failure after recovery start | Use the ordinary truthful provider failure; do not reinterpret it as context recovery success. |
| Browser closed or refreshed | Continue server-side; attach on return if active, otherwise render persisted result. |

## Existing-state adoption

On startup and on the first post-deploy session load, an existing terminal
session with `recommended_action=start_focused_continuation` may be adopted into
same-session recovery only when:

- no focused-continuation child contains substantive work;
- the terminal user turn and session/profile identity can be validated; and
- a trustworthy seed can be built under the new rules.

Adoption changes the marker to the new pending claim and starts through the
same server-owned path. If a focused child already contains work, the source is
left untouched and disclosed as legacy state; the change does not merge or
repeat that work. Empty legacy child cleanup is outside scope.

## Testing strategy

Tests are written first and must fail against the current focused-fork behavior.

### Seed policy

- trusted summary plus exact failed request;
- no summary, but a substantive assistant plan followed by a short authorization
  matching the reported screenshot shape;
- independently substantive user request with no summary/checkpoint;
- generic request with no trustworthy context blocks;
- bounded and redacted assistant content;
- exact user text and safe attachment preservation;
- exclusion of tool results, tool arguments, reasoning, synthetic controls,
  terminal errors, and recovery markers;
- partial assistant text is labeled and bounded.

### Receipt and lifecycle

- duplicate terminal callbacks produce one claim;
- claim is durable before pending recovery is reported;
- parent ownership prevents early successor start;
- post-unregister start uses the same session ID and creates no `Session`;
- duplicate settle calls and multiple tabs start one stream;
- reload with a live successor attaches without starting it again;
- safe crash points before journal submission reclaim once;
- exact launch failure reclaims once;
- ambiguous post-submission crash blocks instead of rerunning;
- startup recovery validates profile/session/fingerprint identity;
- a newer human turn supersedes a claim and the stale control never runs;
- a recovery continuation's own exhaustion cannot loop.

### Transcript and UI

- visible messages before and after seed installation are byte-for-byte
  equivalent apart from the later appended assistant result;
- the original user row appears once and the internal control appears zero
  times in visible history;
- no focused session, title suffix, URL change, sidebar row, read-only banner,
  or action card is produced;
- pending recovery renders quiet live status;
- successful settled history renders no recovery card;
- blocked recovery renders a truthful same-conversation diagnostic;
- desktop, narrow, and mobile views preserve composer access and stream follow.

### Backend siblings and regression

- structured result and exception paths share the same claim helper;
- native and Gateway-backed shared start routing receive the same source,
  context, and attachments;
- ordinary successful compression, goal continuation, tool-limit continuation,
  process wakeups, cancel, retry, and manual forks remain unchanged;
- legacy terminal state with no substantive child adopts once;
- legacy state with substantive child does not repeat work.

All Python tests run through `./scripts/test.sh`. JavaScript behavior uses the
repository's existing JS test harness. The affected and neighboring recovery,
streaming, session-lineage, turn-journal, and UI suites run before the full
repository suite.

## Documentation and live acceptance

Implementation updates the architecture/runtime contracts, testing guidance,
and troubleshooting text that currently tells users to start a focused
continuation. `CHANGELOG.md` remains release-workflow-owned.

Live acceptance on the launchd-managed runtime requires:

1. restart through the checked-in live-main workflow;
2. health and build/source-identity checks;
3. an isolated exhaustion fixture with a summary;
4. the screenshot-shaped fixture with no summary, a prior assistant plan, and a
   short authorization;
5. confirmation that session ID, title, URL, sidebar row, and visible transcript
   remain the same while the successor streams; and
6. restart/reload and duplicate-start evidence.

No push occurs until targeted tests, neighboring tests, the full required test
command, and live health verification pass.

## Implementation ownership

After implementation planning is approved, local Ornith receives one bounded
mechanical slice at a time, beginning with characterization tests and the
smallest shared backend seam. Ornith may edit only the paths named in that
slice. Codex retains scope control, reviews every diff, resolves contract-level
judgment, runs final acceptance, commits on the existing `main`, and pushes
`main` only after verification succeeds.
