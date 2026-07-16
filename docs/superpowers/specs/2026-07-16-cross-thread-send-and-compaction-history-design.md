# Cross-Thread Send and Compaction History Design

## Goal

Prevent a composer draft from being submitted to a different conversation and
make settled conversation history read like a conversation instead of exposing
automatic-compaction machinery.

The repair must preserve `state.db` as the canonical durable transcript, retain
compression lineage for recovery, and avoid rewriting existing user data.

## Observed failure

Two browser chat-start requests overlapped after a WebUI restart while the
visible conversation changed. A Comandero prompt was persisted into an Ornith
session before compaction. Automatic compaction then summarized the already
contaminated transcript into a continuation, making the crossover durable and
more visible.

The settled renderer independently rebuilds context-compaction and preserved
task-list rows from transcript metadata. On a long compressed lineage this
produces repeated `Context compaction` and `Preserved task list` cards even
though the UI contract says successful automatic compression is live-only.
The same captured mobile view exposed raw leading workspace sentinels in user
bubbles, so display-prefix hardening is part of this presentation repair.

## Constraints

- Do not delete, merge, or rewrite existing `state.db` messages.
- Do not make WebUI JSON sidecars canonical.
- Do not disable compression or remove recovery lineage.
- Old clients that do not send the new optional ownership field remain
  compatible.
- Manual `/compress` history may keep its explicit reference card.
- Successful automatic compression is omitted after settlement; recovery and
  error states remain visible.
- Keep the vanilla JavaScript and Python architecture.

## Considered approaches

### 1. Client-only active-session check

Capture the active session immediately before `POST /api/chat/start` and abort
if the pane changed.

This is small, but it cannot distinguish a stale draft that has already leaked
into the newly active pane. It also gives the server no evidence when an older
or racing client sends contradictory ownership metadata.

### 2. Change compression to in-place storage

Enable in-place compression and remove continuation rows.

This reduces lineage visibility but does not prevent the initial wrong-session
write. It also changes recovery and durability semantics in Hermes Agent, which
is a much larger blast radius than the reported failure requires.

### 3. Draft ownership plus settled-render filtering

Bind the visible composer payload to one session ID, carry that owner through
save, restore, queue, and send, and include it as an optional
`composer_session_id` in chat-start requests. The server rejects a request when
the supplied owner differs from `session_id`. Separately, settled rendering
omits successful automatic-compression references and preserved task-list
markers while retaining live compression and explicit manual compression.

This is the chosen approach. It protects the mutation boundary and fixes the
presentation contract without changing durable lineage.

## Composer ownership

The browser keeps one explicit composer owner:

- loading or restoring a session assigns the composer to that session;
- a user input event assigns newly edited content to the currently visible
  session;
- programmatic draft restoration assigns the restored draft to its target
  session;
- clearing the composer clears payload but retains the visible session as the
  empty composer's owner;
- switching away saves the draft only for the recorded owner;
- `send()` captures both the payload and owner before any await;
- if owner and target differ, no optimistic message or chat-start request is
  created. The draft remains recoverable under its owner and the user sees an
  explicit warning.

Queue and busy-send paths use the same captured owner rather than deriving a
new target after an asynchronous boundary.

The chat-start payload includes `composer_session_id`. The field is optional for
compatibility, but when present the server requires an exact match with
`session_id` before loading or mutating a session. A mismatch returns `409` and
logs IDs only; prompt text is never logged.

## Settled compression rendering

Automatic compression remains visible while active as the existing quiet
divider. Once the turn settles:

- raw context-compaction messages remain excluded from normal message rows;
- automatic compression summaries do not become reference cards;
- preserved task-list markers do not become cards;
- successful automatic compression contributes no standalone settled row;
- manual compression keeps its user-requested reference card;
- compression-exhausted, recovery, degraded, and error states keep their
  existing visible status surfaces.

The renderer reads `compression_anchor_mode`. `manual` is the only mode that
opts into settled reference/task-list cards. Unknown or legacy modes fail quiet
because synthetic recovery material is lower priority than the conversation.

## Workspace sentinel display

User-facing message rendering continues stripping the workspace sentinel. The
helper is hardened to remove repeated leading v1 or legacy sentinels so a
recovered or stitched message cannot expose one merely because multiple layers
prefixed it. Durable content is not modified.

## State layers and invariants

- **Canonical transcript:** `state.db`; unchanged by this repair except for
  correctly targeted future turns.
- **Composer draft:** session-scoped UI state; gains explicit browser ownership.
- **Chat-start mutation:** server validates optional composer ownership before
  session lookup or persistence.
- **Compression summary/task list:** agent-facing recovery material; not current
  user intent and not settled conversation content.
- **Live compression divider:** transient UI state; unchanged.

Required invariants:

1. A draft owned by session A cannot start or queue a turn in session B.
2. Matching owners, and old clients that omit the optional field, start normally.
3. Rapid session switching never saves visible text under the wrong session.
4. Settled automatic compression adds no context/task-list cards.
5. Manual compression and visible recovery/error surfaces remain available.
6. Workspace sentinels never appear in user bubbles, including repeated prefixes.

## Verification

Automated tests will cover:

- client send rejection before optimistic UI and `/api/chat/start` on owner
  mismatch;
- matching-owner payload wiring;
- backend `409` on mismatch and backward compatibility when omitted;
- draft save/restore ownership across rapid switches;
- settled automatic versus manual compression rendering;
- repeated workspace-prefix stripping;
- focused session, chat-start, compression, and message-render regression shards.

Manual verification will use isolated state for a rapid switch/send scenario,
then the launchd-managed live WebUI will be restarted and checked through its
health endpoint and a narrow/mobile browser render. Existing contaminated rows
will not be rewritten in this change.
