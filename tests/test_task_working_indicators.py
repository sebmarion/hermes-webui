"""Regression checks for task working indicators in the sidebar and chat."""

import re
from pathlib import Path


STYLE_CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(
    encoding="utf-8"
)


def _media_block(start_marker: str, end_marker: str) -> str:
    start = STYLE_CSS.find(start_marker)
    assert start != -1, f"missing CSS block: {start_marker}"
    end = STYLE_CSS.find(end_marker, start + len(start_marker))
    assert end != -1, f"missing CSS block terminator: {end_marker}"
    return STYLE_CSS[start:end]


def test_sidebar_working_indicator_survives_desktop_action_states():
    """The working spinner must stay visible when row actions are revealed."""
    assert "@media (hover:hover)" in STYLE_CSS
    assert re.search(
        r"\.session-item:hover \.session-attention-indicator\.is-streaming,\s*"
        r"\.session-item:focus-within \.session-attention-indicator\.is-streaming,\s*"
        r"\.session-item\.menu-open \.session-attention-indicator\.is-streaming\{"
        r"opacity:1;visibility:visible;right:34px;\}",
        STYLE_CSS,
    )

    touch_block = _media_block(
        "@media (hover:none) and (pointer:coarse){\n    .session-actions{display:none;}",
        "@media (max-width: 340px){",
    )
    assert ".session-actions{display:none;}" in touch_block
    assert ".session-item.streaming,.session-item.unread{padding-right:40px;}" in touch_block
    assert "right:34px;" not in touch_block


def test_live_worklog_dot_is_visible_and_pulses():
    """Only settled worklogs hide their dot; the current live group advertises work."""
    hide_rule = (
        '.tool-worklog-group[data-tool-worklog-group="1"]'
        ':not([data-run-activity-group="1"]):not([data-live-tool-call-group="1"]) .as-dot'
    )
    assert hide_rule in STYLE_CSS

    live_dot_rule = (
        '.tool-worklog-group[data-live-tool-call-group="1"] .as-dot{'
        "display:block;background:var(--accent);opacity:.78;"
        "animation:pulse 1.4s ease-in-out infinite;}"
    )
    assert live_dot_rule in STYLE_CSS


def test_live_worklog_dot_respects_reduced_motion_without_disappearing():
    reduced_motion_rule = (
        '@media (prefers-reduced-motion:reduce){\n'
        '  .tool-worklog-group[data-live-tool-call-group="1"] .as-dot{\n'
        "    animation:none;\n"
        "    opacity:.78;\n"
        "  }\n"
        "}"
    )
    assert reduced_motion_rule in STYLE_CSS
