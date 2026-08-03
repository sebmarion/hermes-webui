"""Compression-exhausted recovery metadata and intent helpers."""

from __future__ import annotations

import re
import time
from typing import Any


COMPRESSION_RECOVERY_TERMINAL_STATE = "compression_exhausted"
COMPRESSION_RECOVERY_ACTION_START_FOCUSED = "start_focused_continuation"
COMPRESSION_RECOVERY_CONTEXT_MAX_CHARS = 8_000
COMPRESSION_RECOVERY_DRAFT_MAX_CHARS = 50_000

_RECOVERY_DRAFT_NOTE = (
    "Context recovery note: inspect the current workspace and existing results "
    "before repeating any action."
)


_GENERIC_CONTINUATION_INTENTS = frozenset(
    {
        "continue",
        "continue please",
        "go on",
        "keep going",
        "resume",
        "proceed",
        "carry on",
        "继续",
        "继续吧",
        "接着",
        "接着做",
        "继续做",
        "继续执行",
    }
)


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _normalize_intent_text(text: str) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    # Keep CJK letters while dropping punctuation/emoji/noise around short intents.
    return re.sub(r"[\W_]+", " ", raw, flags=re.UNICODE).strip()


def is_generic_continuation_intent(text: str) -> bool:
    """Return True only for short, content-free continuation requests."""

    normalized = _normalize_intent_text(text)
    if normalized in _GENERIC_CONTINUATION_INTENTS:
        return True
    # A single repeated word such as "continue continue" is still generic, but
    # longer prompts like "continue by summarizing file X" are substantive.
    parts = normalized.split()
    return bool(parts) and len(parts) <= 2 and all(
        part in _GENERIC_CONTINUATION_INTENTS for part in parts
    )


def _message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or part.get("content") or "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"text", "input_text", "output_text"}
        ).strip()
    return str(content or "").strip()


def _latest_compressed_summary(session) -> dict | None:
    # The model-facing list owns the active summary. Visible messages are only a
    # fallback for older sidecars that did not preserve context_messages.
    for attr in ("context_messages", "messages"):
        messages = getattr(session, attr, None)
        if not isinstance(messages, list):
            continue
        for message in reversed(messages):
            if not isinstance(message, dict) or not message.get("_compressed_summary"):
                continue
            if message.get("role") != "tool" and _message_text(message):
                return message
    return None


def _is_synthetic_or_recovery_user_turn(message: dict, text: str) -> bool:
    if any(
        message.get(flag)
        for flag in (
            "_compressed_summary",
            "_internal",
            "_synthetic",
            "_recovery_control",
            "_tool_limit_continuation_control",
        )
    ):
        return True
    source = str(
        message.get("_source")
        or message.get("source")
        or message.get("source_tag")
        or ""
    ).strip().lower()
    if source in {
        "tool_limit_continuation",
        "process_wakeup",
        "cron-recovery",
        "compression_recovery",
        "goal_continuation",
    }:
        return True
    normalized = _normalize_intent_text(text)
    if is_generic_continuation_intent(text) or normalized in {
        "go",
        "handoff",
        "make a handoff",
        "start focused continuation",
    }:
        return True
    lowered = text.lower().lstrip()
    return lowered.startswith(
        (
            "[context compaction",
            "[context compression",
            "[prior context",
            "[your active task list was preserved",
            "[hermes_tool_limit_continuation]",
            "[async delegation",
            "[compression recovery",
        )
    )


def _latest_substantive_user_request(session) -> str:
    # Visible transcript order is authoritative. Model-facing context is only a
    # fallback for repaired/legacy sidecars with no display rows.
    for attr in ("messages", "context_messages"):
        messages = getattr(session, attr, None)
        if not isinstance(messages, list):
            continue
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            text = _message_text(message)
            if text and not _is_synthetic_or_recovery_user_turn(message, text):
                return text
    return ""


def _bounded_recovery_text(text: str, limit: int) -> str:
    value = str(text or "")
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    separator = "\n\n[Recovery summary bounded to fit the fresh context.]\n\n"
    if limit <= len(separator):
        return value[:limit]
    remaining = limit - len(separator)
    head = remaining // 2
    tail = remaining - head
    return value[:head] + separator + value[-tail:]


def build_focused_continuation_seed(session) -> tuple[list[dict], dict]:
    """Return one hidden summary reference and one durable unsent task draft."""

    context_messages = []
    summary = _latest_compressed_summary(session)
    if summary is not None:
        # This seed crosses into a fresh model context. Redact regardless of the
        # optional API display setting, then drop every source-message field
        # except the safe role/content marker contract.
        from api.helpers import _redact_text

        summary_text = _redact_text(_message_text(summary), _enabled=True)
        context_messages.append(
            {
                "role": "assistant",
                "content": _bounded_recovery_text(
                    summary_text,
                    COMPRESSION_RECOVERY_CONTEXT_MAX_CHARS,
                ),
                "_compressed_summary": True,
                "_compression_recovery_reference": True,
            }
        )

    latest_request = _latest_substantive_user_request(session)
    if latest_request:
        draft_text = f"Continue: {latest_request}\n\n{_RECOVERY_DRAFT_NOTE}"
    else:
        draft_text = (
            "Continue the unfinished task from this conversation.\n\n"
            f"{_RECOVERY_DRAFT_NOTE}"
        )
    composer_draft = {
        "text": _bounded_recovery_text(
            draft_text,
            COMPRESSION_RECOVERY_DRAFT_MAX_CHARS,
        ),
        "files": [],
    }
    return context_messages, composer_draft


def build_compression_recovery_payload(session, *, message: str = "", details: str = "") -> dict:
    """Build the durable UI/route payload for a compression-exhausted turn."""

    source_sid = str(getattr(session, "session_id", "") or "")
    last_prompt_tokens = _positive_int(getattr(session, "last_prompt_tokens", None))
    threshold_tokens = _positive_int(getattr(session, "threshold_tokens", None))
    context_length = _positive_int(getattr(session, "context_length", None))
    payload = {
        "terminal_state": COMPRESSION_RECOVERY_TERMINAL_STATE,
        "recommended_action": COMPRESSION_RECOVERY_ACTION_START_FOCUSED,
        "source_session_id": source_sid,
        "created_at": time.time(),
        "title": "Context compression exhausted",
        "summary": (
            "This run could not safely shrink the conversation enough to continue in place. "
            "Open a focused continuation with a recovered task draft."
        ),
        "action_label": "Start focused continuation",
        "message": str(message or "").strip(),
        "details": str(details or "").strip()[:1200],
        "last_prompt_tokens": last_prompt_tokens,
        "threshold_tokens": threshold_tokens,
        "context_length": context_length,
    }
    return payload


def stamp_compression_exhausted_recovery(session, *, message: str = "", details: str = "") -> dict:
    """Persist recovery metadata on a session and return the message payload."""

    payload = build_compression_recovery_payload(session, message=message, details=details)
    session.recommended_recovery_action = payload["recommended_action"]
    session.compression_recovery = payload
    return payload


def compression_recovery_payload_for_session(session) -> dict | None:
    payload = getattr(session, "compression_recovery", None)
    if not isinstance(payload, dict):
        return None
    if payload.get("terminal_state") != COMPRESSION_RECOVERY_TERMINAL_STATE:
        return None
    action = str(payload.get("recommended_action") or getattr(session, "recommended_recovery_action", "") or "")
    if action != COMPRESSION_RECOVERY_ACTION_START_FOCUSED:
        return None
    return payload


def clear_compression_recovery(session) -> None:
    session.recommended_recovery_action = None
    session.compression_recovery = {}
