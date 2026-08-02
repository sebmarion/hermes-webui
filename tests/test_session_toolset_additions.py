"""Regression coverage for additive per-session tool capabilities."""

from pathlib import Path

from api.config import _configured_session_toolsets, _merge_session_toolsets


REPO = Path(__file__).resolve().parents[1]


def test_session_gitnexus_selection_preserves_operator_tools():
    """Selecting one MCP server must not replace the profile's core tools."""
    profile_defaults = [
        "clarify",
        "code_execution",
        "delegation",
        "file",
        "memory",
        "session_search",
        "skills",
        "terminal",
        "todo",
        "web",
    ]

    effective = _merge_session_toolsets(profile_defaults, ["gitnexus"])

    assert effective == [*profile_defaults, "gitnexus"]
    assert "file" in effective
    assert "terminal" in effective


def test_session_toolset_additions_are_ordered_and_deduplicated():
    assert _merge_session_toolsets(
        ["file", "terminal", "gitnexus"],
        ["gitnexus", "skills", "terminal"],
    ) == ["file", "terminal", "gitnexus", "skills"]


def test_session_additions_are_limited_to_enabled_configured_mcp_servers():
    cfg = {
        "mcp_servers": {
            "gitnexus": {"command": "gitnexus", "enabled": True},
            "disabled-server": {"command": "disabled", "enabled": False},
            "invalid-server": {"enabled": True},
        }
    }
    allowed = _configured_session_toolsets(cfg)

    assert allowed == {"gitnexus"}
    assert _merge_session_toolsets(
        ["file", "terminal"],
        ["gitnexus", "terminal", "unknown"],
        allowed_session_toolsets=allowed,
    ) == ["file", "terminal", "gitnexus"]


def test_absent_session_additions_leave_profile_defaults_unchanged():
    assert _merge_session_toolsets(["file", "terminal"], None) == ["file", "terminal"]


def test_streaming_uses_the_additive_merge_at_the_agent_boundary():
    streaming = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    decision_start = streaming.index("# Per-session toolset additions (#493)")
    decision_end = streaming.index("# Fallback model chain", decision_start)
    decision = streaming[decision_start:decision_end]

    assert "_merge_session_toolsets(" in decision
    assert "allowed_session_toolsets=_allowed_session_toolsets" in decision
    assert "_toolsets = _override" not in decision
