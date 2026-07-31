"""Browser contract coverage for compact successful terminal SSE payloads."""

from pathlib import Path


MESSAGES_JS = (
    Path(__file__).resolve().parents[1] / "static" / "messages.js"
).read_text(encoding="utf-8")


def _done_handler() -> str:
    start = MESSAGES_JS.index("source.addEventListener('done'")
    end = MESSAGES_JS.index("source.addEventListener('stream_end'", start)
    return MESSAGES_JS[start:end]


def test_done_reconciles_only_terminal_delta_instead_of_replacing_full_transcript():
    done = _done_handler()

    assert "function _reconcileTerminalDoneMessages(" in MESSAGES_JS
    assert "_reconcileTerminalDoneMessages(S.messages||[],completedSession,_currentDoneOffset)" in done
    assert "d.session.messages=_nextDoneMessages" in done
    assert "S.messages=_carryForwardEphemeralTurnFields(S.messages||[], d.session.messages||[])" in done
    assert "terminal_base_message_count" in MESSAGES_JS
    assert "terminal_messages" in MESSAGES_JS


def test_done_preserves_live_messages_and_accepts_explicit_full_fallback_on_prefix_mismatch():
    assert "session.terminal_reconcile_required" in MESSAGES_JS
    assert "Array.isArray(completedSession.messages)" in MESSAGES_JS
