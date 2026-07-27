# Compressed Session Sidebar Deduplication

## Problem

Automatic compression can rotate one logical conversation from a physical
parent session to a continuation session. The sidebar cache may still contain
the parent while opening that row resolves through `/api/session` to the
continuation. The browser then injects the active continuation as an ephemeral
row without removing the requested parent, so the same conversation appears
twice. The parent owns the live spinner and the continuation owns the selected
state, which makes both titles yellow.

## Chosen Approach

Use the explicit identity mapping returned by the session detail endpoint.
When `S.session.session_id` is absent from the sidebar rows and
`S.session.requested_session_id` identifies an existing row:

1. Replace the requested row in place with one row whose identity is the
   canonical active session.
2. Preserve live runtime fields from the requested physical segment when the
   canonical detail does not carry them.
3. Prefer canonical conversation metadata, including the canonical message
   count, without summing compression segments.
4. Preserve the row's position so selection does not reorder the sidebar.

This changes only the browser's sidebar presentation layer. Durable
conversation identity remains owned by `state.db`; runtime state remains an
overlay and is not written back to conversation metadata.

## Alternatives Considered

### Backend-only canonicalization

The shared sidebar projection should normally return the continuation, but a
browser can still hold a stale parent row between list refreshes. A backend-only
change would not repair that already-cached transition.

### Deduplicate matching titles

Different conversations can legitimately share a title. Title-based
deduplication would hide unrelated sessions and is therefore rejected.

### Force a full sidebar reload after every canonical redirect

This adds a network round trip and can still flicker between two rows. It is
broader than replacing the one alias proven by the detail response.

## Data Flow

```text
stale sidebar parent row
          +
loaded canonical session
(requested parent -> canonical continuation)
          |
          v
replace alias in cached render rows
          |
          v
one active + streaming canonical row
```

## Error Handling

If there is no explicit requested/canonical mapping, the existing behavior is
unchanged: a genuinely new zero-message session is injected as an ephemeral
row. Rows are never merged based on title, parent ID alone, or guessed lineage.

## Verification

Add a JavaScript helper regression test covering the observed state:

- cached parent row has a live stream and pending message;
- loaded active session is the canonical continuation;
- the detail payload names the parent as `requested_session_id`;
- the result contains exactly one row;
- that row uses the continuation ID and message count;
- that row retains the parent's live stream state.

Run the focused sidebar tests through `./scripts/test.sh`, then run the broader
session-lineage and bounded-detail suites. Finally, inspect the diff with
GitNexus `detect_changes()` and verify no unrelated files or flows changed.
