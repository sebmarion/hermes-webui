# Task Working Indicators Design

## Problem

While a task is running, Hermes WebUI no longer gives a reliable visible
working signal. The sidebar conversation row can lose its running indicator
when the row is hovered or focused, and the compact in-chat worklog creates an
activity dot that current CSS hides. This makes an active task look idle.

The backend already exposes the relevant state: `is_streaming` for WebUI-owned
streams and `is_working` for fresh shared `session_activity` heartbeats. The
change should preserve that state contract and repair the presentation gates.

## Goals

- Make an active task visibly identifiable in the sidebar.
- Keep the sidebar working indicator visible while row actions are exposed,
  without overlapping the action trigger.
- Restore a visible, animated working dot in the live compact worklog.
- Keep settled activity rows quiet; do not turn historical worklog rows into
  active indicators.
- Add regression coverage for both surfaces.

## Non-goals

- No changes to the `state.db` schema, heartbeat TTL, session projection, or
  streaming lifecycle.
- No new global banner, notification, or composer state model.
- No redesign of sidebar actions or the compact worklog.

## Design

### Sidebar

The existing row state calculation remains authoritative:

`_isSessionEffectivelyStreaming(session)` treats `is_streaming`, `is_working`,
fresh pending-user state, and the active local stream as working state.

The existing right-side state indicator remains the working glyph. When the row
is idle, it keeps the current hover/focus behavior that hides unread and
attention markers while actions are visible. When the row is working, the
indicator stays visible during hover, focus, and menu-open states. On desktop
mouse-capable layouts only (`@media (hover:hover)`), those states move the
working indicator from `right:6px` to `right:34px`; this reserves the existing
26px action-trigger slot plus the existing 2px visual gap. Touch layouts keep
the indicator at `right:6px` because `.session-actions` is hidden there, so a
focus state does not create a false offset.

### In-chat worklog

The live compact worklog is identified by
`data-live-tool-call-group="1"`. Its `.as-dot` remains visible and uses the
existing `pulse 1.4s ease-in-out infinite` animation with the accent color.
The generic settled-worklog suppression selector becomes
`.tool-worklog-group[data-tool-worklog-group="1"]:not([data-run-activity-group="1"]):not([data-live-tool-call-group="1"]) .as-dot`,
so it continues to hide `.as-dot` for non-live worklog groups while leaving
the live group visible. Under `prefers-reduced-motion: reduce`, the live dot
keeps its visible size/color but disables the pulse animation.

## Data flow and invariants

`state.db` / WebUI runtime state → `/api/sessions` sidebar payload →
`_isSessionEffectivelyStreaming` → sidebar row state class and indicator.

SSE/live-turn state → live compact worklog group → `.as-dot` visibility.

The following invariants remain unchanged:

- Fresh `session_activity` heartbeats are the only cross-surface source for
  `is_working`.
- Stale heartbeats do not produce a working marker.
- A completed turn removes or settles the live worklog and clears the working
  sidebar state.
- Hovering the sidebar must not make an active task indistinguishable from an
  idle conversation.

## Testing

- Add a static regression test that verifies a working sidebar indicator is not
  hidden by hover/focus/menu rules, shifts to `right:34px` only in the desktop
  hover media query, and stays at `right:6px` on touch layouts.
- Add a static regression test that verifies the live worklog dot is excluded
  from the settled-worklog hide selector, uses the pulse animation, and has a
  reduced-motion override.
- Run the focused sidebar indicator, worklog, and shared-session-activity tests.
- Run the repository JavaScript runtime lint/static checks available in the
  project, plus `git diff --check`.
- Perform a browser check at desktop and touch-sized viewports: start a task,
  hover/focus/open the sidebar row actions, and confirm the working indicator
  remains visible without overlap; confirm the live in-chat dot is visible and
  that reduced-motion mode disables only its animation. On touch-sized
  viewports, use focus and long-press/action-menu states rather than hover,
  since sidebar actions are hidden for coarse pointers.

## Rollback

Revert the frontend CSS/test changes. No durable state or migration is involved.
