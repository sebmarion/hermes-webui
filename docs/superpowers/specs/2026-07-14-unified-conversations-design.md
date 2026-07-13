# Unified Conversations Design

## Goal

Make Hermes One and Hermes WebUI show the same interactive conversations and
the same shared metadata. Archiving, unarchiving, pinning, or renaming an
interactive conversation in either surface must be reflected in the other
surface without importing it or switching sidebar tabs.

The WebUI sidebar becomes one chronologically ordered conversation list.
Source remains visible as a badge and continues to control capabilities such
as read-only behavior, but it no longer partitions interactive conversations
into separate WebUI and CLI views.

## Current failure

Hermes One writes interactive session metadata to the profile's `state.db`.
WebUI currently projects only `source = webui` rows through its canonical
state-db-first path. CLI, TUI, and ACP rows take a separate compatibility path
that normally reads only the 20 newest rows and derives archive state from an
optional WebUI JSON sidecar.

That creates two incorrect outcomes:

- older Hermes One conversations disappear before archived pagination runs;
- a state-db row archived in Hermes One can appear unarchived in WebUI when it
  has no matching sidecar.

The browser then reinforces the split by always requesting either
`sidebar_source=webui` or `sidebar_source=cli` and rendering source tabs.

## Chosen architecture

### One canonical interactive projection

Generalize the existing state-db-first WebUI projection into a shared
interactive projection. It reads all interactive source families for the
active profile:

- `webui`
- `cli`
- `tui`
- `acp`

The projection uses the existing logical-session resolution rules, so
compression segments collapse to one visible conversation and delegates,
tool-only children, and other non-interactive children retain their existing
visibility behavior.

For every canonical interactive conversation, `state.db` owns:

- identity and lineage;
- title;
- working directory/workspace;
- archive and pin state;
- source metadata;
- timestamps and message counts.

WebUI mutations remain bidirectional. The existing rename/title-regeneration,
archive/unarchive, pin/unpin, and workspace mutation paths must write the
result through the state-sync helpers to the target profile's `state.db` and
invalidate the session-list projection. This applies both to native WebUI
rows and to an interactive CLI/TUI/ACP row that WebUI materializes for a
supported mutation. Sidecar persistence may still provide recovery, but a
successful WebUI response must not leave shared metadata sidecar-only.

A matching WebUI sidecar may overlay only WebUI runtime or presentation data,
including active stream recovery, pending composer state, worktree
presentation, token/cost presentation, and other fields that have no shared
state-db contract. A sidecar must not override a non-empty canonical title,
archive state, pin state, workspace, source, or lineage.

The projection preserves each row's raw source and normalized source label.
It also marks the row as a canonical shared interactive row so downstream
compatibility code can avoid re-capping or replacing it.

### Merge and compatibility behavior

The canonical interactive projection is included regardless of the stored
`show_cli_sessions` setting. That compatibility setting must no longer hide
Hermes One, CLI, TUI, or ACP conversations from the shared list.

The existing auxiliary loader remains for sources that are not part of the
canonical interactive set, such as Claude Code and source-specific background
or channel rows. Existing cron, webhook, messaging, project, hidden-session,
and child-session visibility rules remain in force. If the auxiliary loader
also returns an interactive row, the canonical projection wins by canonical
identity and lineage aliases.

The stored `show_cli_sessions` key remains readable for compatibility, but
the settings UI describes only the optional external sessions it still
controls. Removing or migrating the stored key is out of scope.

All-profiles mode applies the same state-db archive/title/pin authority per
profile. Where that mode still uses the auxiliary projection, its state-db
metadata must be used instead of sidecar archive defaults.

### Sidebar and API contract

The current WebUI stops sending `sidebar_source`, removes the WebUI/CLI source
tabs, and renders all returned interactive rows together. Existing source
badges, source-specific actions, read-only behavior, project filters, search,
and active-session handling remain.

`GET /api/sessions` without `sidebar_source` is the primary contract:

- all non-archived matching rows are returned;
- archived rows are globally ordered with the same list and limited only by
  `archived_limit` when that parameter is present;
- `archived_count` is the authoritative combined count before archived-page
  slicing;
- no 20-row CLI/TUI cap is applied to canonical interactive rows.

The backend continues accepting `sidebar_source=webui|cli` for older clients.
For compatibility, `sidebar_source=cli` means the existing combined
CLI/TUI/ACP family; it does not introduce separate filters for those raw
sources.
Compatibility counts such as `archived_webui_count` and
`archived_cli_count` may remain in the response, but the current browser does
not use them to partition the list.

Archived search and project-filter behavior remains as it is today: those
scopes may request the uncapped archived result set. The normal archive
expander increases `archived_limit` over the one combined archive.

The request path retains the existing bounded cold-start and
stale-while-revalidate cache contract. It must not synchronously block the
first sidebar response on a full state-db reconciliation. An index-only cold
seed or a stale last-known-good payload may therefore be temporarily
incomplete, but the background full builder must read the uncapped canonical
interactive projection, publish complete archive counts, and replace that
seed. Existing state-db cache fingerprints/TTLs detect Hermes One writes;
WebUI metadata mutations also publish explicit invalidations. The allowed lag
is the repository's existing bounded cache window, never permanent omission
from a 20-row compatibility cap.

Pinned/date grouping, ordering within those groups, and transient sidecar-only
active or first-turn safeguards remain unchanged after rows are unified.

### Legacy WebUI sidecars

The existing legacy archive remains available for messageful WebUI sidecars
that have no state-db row. Empty sidecars remain ignored. This compatibility
bucket is rendered below the canonical archive and does not become a second
source tab.

Deleted-WebUI tombstones, one-time legacy pin migration, live activity
overlays, and state-db-unavailable degradation retain their existing safety
behavior.

## Data flow

1. Read and logically resolve all interactive rows from the profile's
   `state.db` without a visible-row cap.
2. Overlay matching live WebUI runtime/presentation sidecars.
3. Preserve source-specific and legacy rows through the existing auxiliary
   paths.
4. Deduplicate by canonical identity and every lineage alias, preferring the
   canonical interactive row.
5. Apply profile, hidden, project, search, and source-specific visibility
   rules.
6. Compute the combined archived count, then apply `archived_limit` to the
   globally ordered archived rows.
7. Render one browser list with source badges.

The data flow above describes the full cache builder. The bounded cold seed
may serve step 7 from the immutable index while one background owner performs
steps 1-6 and atomically replaces the cached response.

## Failure handling

If `state.db` is missing, locked, or unreadable, WebUI degrades to the current
sidecar/auxiliary rows and logs the projection failure. It must not mutate or
delete real Hermes state as part of list loading. A sidecar write racing with
the projection may contribute runtime fields, but cannot reverse canonical
archive, pin, title, or workspace state.

## Non-goals

- Synchronizing databases between machines.
- Changing how Hermes One persists session metadata.
- Merging cron, webhook, messaging, tool-only, or subagent rows into the
  interactive source family.
- Removing legacy source-filter API parameters or stored settings in this
  change.
- Replacing source badges or source-specific action/read-only rules.

## Verification

Automated coverage must prove:

- CLI/TUI/ACP and WebUI rows are returned by the same canonical projection;
- a Hermes One archive bit in `state.db` is reflected without a sidecar;
- canonical title, pin, workspace, source, and lineage beat stale sidecar
  metadata;
- WebUI rename, archive/unarchive, pin/unpin, and workspace mutations write
  shared metadata through to the correct profile's `state.db`;
- more than 20 interactive rows remain countable and archived pagination can
  reach the older rows;
- the current browser omits `sidebar_source`, has no source tabs, and uses one
  combined archive count/expander;
- legacy `sidebar_source` requests still filter for old clients;
- legacy sidecar archives and non-interactive source visibility still work;
- all-profiles projection preserves state-db archive authority.
- an index-only cold seed remains non-blocking and is replaced by an uncapped
  canonical background rebuild after state-db or mutation invalidation.

Run focused regression tests through `./scripts/test.sh`, then run the
repository's relevant session-list and frontend test groups. Perform a manual
desktop and narrow/mobile browser check against isolated state directories,
including archiving in one surface and observing the other after refresh.
