"""Compression-exhausted recovery metadata and intent helpers."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
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


class CompressionRecoveryBlocked(ValueError):
    """Fail-closed result when no safe same-session recovery can be built."""

    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(self.reason)


_RECOVERY_ASSISTANT_UNSAFE_FLAGS = (
    "_compressed_summary",
    "_error",
    "_internal",
    "_synthetic",
    "_recovery_control",
    "_compression_recovery_reference",
    "_tool_limit_continuation_control",
    "_goal_continuation_control",
    "_managed_continuation_control",
)

_RECOVERY_CONTROL_PREFIXES = (
    "[compression recovery",
    "[context compression",
    "[context compaction",
    "[prior context",
    "[hermes_tool_limit_continuation]",
    "[your active task list was preserved",
)

_RECOVERY_DEICTIC_MARKERS = (
    " do it",
    " do that",
    " do this",
    "handle it",
    "handle that",
    "handle this",
    "you said",
    "you mentioned",
    "the other steps",
    "those steps",
    "as above",
)


def _recovery_block(reason: str) -> CompressionRecoveryBlocked:
    return CompressionRecoveryBlocked(reason)


def validate_recovery_attachments_for_use(
    attachments: list[dict] | None,
) -> list[dict]:
    """Return safe attachment copies or block rather than silently dropping one."""

    if attachments is None:
        return []
    if not isinstance(attachments, list):
        raise _recovery_block("attachment_invalid")
    if len(attachments) > 20:
        raise _recovery_block("attachment_limit")

    normalized: list[dict] = []
    identities_by_name: dict[str, tuple[str, str, int | None, bool | None]] = {}
    for raw in attachments:
        if not isinstance(raw, dict):
            raise _recovery_block("attachment_invalid")

        name_value = raw.get("name")
        path_value = raw.get("path")
        mime_value = raw.get("mime")
        if not all(isinstance(value, str) and value.strip() for value in (name_value, path_value, mime_value)):
            raise _recovery_block("attachment_invalid")

        name = name_value.strip()
        path = path_value.strip()
        mime = mime_value.strip()
        candidate = Path(path)
        if not candidate.is_absolute():
            raise _recovery_block("attachment_invalid")
        if not candidate.is_file():
            raise _recovery_block("attachment_missing")

        size: int | None = None
        if "size" in raw:
            raw_size = raw.get("size")
            if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
                raise _recovery_block("attachment_invalid")
            size = raw_size

        is_image: bool | None = None
        if "is_image" in raw:
            raw_is_image = raw.get("is_image")
            if not isinstance(raw_is_image, bool):
                raise _recovery_block("attachment_invalid")
            is_image = raw_is_image

        identity = (path, mime, size, is_image)
        existing = identities_by_name.get(name)
        if existing is not None:
            if existing != identity:
                raise _recovery_block("attachment_conflict")
            continue
        identities_by_name[name] = identity

        item: dict[str, Any] = {"name": name, "path": path, "mime": mime}
        if size is not None:
            item["size"] = size
        if is_image is not None:
            item["is_image"] = is_image
        normalized.append(item)
    return normalized


def _is_safe_assistant_checkpoint(message: dict) -> bool:
    if message.get("role") != "assistant":
        return False
    if any(message.get(flag) for flag in _RECOVERY_ASSISTANT_UNSAFE_FLAGS):
        return False
    if message.get("tool_calls") or message.get("tool_call_id"):
        return False
    if message.get("reasoning") or message.get("reasoning_content"):
        return False
    text = _message_text(message)
    if not text or text.lower().lstrip().startswith(_RECOVERY_CONTROL_PREFIXES):
        return False
    words = _normalize_intent_text(text).split()
    return len(text) >= 40 and len(words) >= 6


def _latest_safe_assistant_checkpoint_before_request(
    session,
    failed_user_text: str,
) -> dict | None:
    """Use visible order, and never borrow assistant output after the failed row."""

    for attr in ("messages", "context_messages"):
        messages = getattr(session, attr, None)
        if not isinstance(messages, list):
            continue
        boundary = len(messages)
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if (
                isinstance(message, dict)
                and message.get("role") == "user"
                and _message_text(message) == failed_user_text
            ):
                boundary = index
                break
        for message in reversed(messages[:boundary]):
            if isinstance(message, dict) and _is_safe_assistant_checkpoint(message):
                return message
    return None


def _is_independently_substantive_recovery_request(text: str) -> bool:
    normalized = _normalize_intent_text(text)
    if not normalized or is_generic_continuation_intent(text):
        return False
    padded = f" {normalized}"
    if len(normalized) <= 160 and any(marker in padded for marker in _RECOVERY_DEICTIC_MARKERS):
        return False
    return len(text.strip()) >= 40 and len(normalized.split()) >= 6


def _safe_partial_recovery_text(text: str) -> str:
    value = str(text or "").strip()
    if not value or value.lower().lstrip().startswith(_RECOVERY_CONTROL_PREFIXES):
        return ""
    from api.helpers import _redact_text

    return "Partial, unverified work:\n\n" + _redact_text(value, _enabled=True)


def _recovery_fingerprint(
    *,
    session_id: str,
    parent_run_id: str,
    context_messages: list[dict],
    attachments: list[dict],
) -> str:
    canonical = json.dumps(
        {
            "session_id": session_id,
            "parent_run_id": parent_run_id,
            "context_messages": context_messages,
            "attachments": attachments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_same_session_recovery_seed(
    session,
    *,
    parent_run_id: str,
    failed_user_text: str,
    attachments: list[dict] | None = None,
    partial_assistant_text: str = "",
) -> dict:
    """Build a bounded, hidden recovery context without mutating the transcript."""

    from api.helpers import _redact_text

    request_text = str(failed_user_text or "")
    if len(request_text) > COMPRESSION_RECOVERY_CONTEXT_MAX_CHARS:
        raise _recovery_block("user_request_exceeds_context_budget")

    summary = _latest_compressed_summary(session)
    checkpoint = None
    trust_source = ""
    trusted_text = ""
    if summary is not None and summary.get("role") == "assistant":
        trust_source = "summary"
        trusted_text = _redact_text(_message_text(summary), _enabled=True)
    else:
        checkpoint = _latest_safe_assistant_checkpoint_before_request(
            session,
            request_text,
        )
        if checkpoint is not None:
            trust_source = "assistant_checkpoint"
            trusted_text = _redact_text(_message_text(checkpoint), _enabled=True)
        elif _is_independently_substantive_recovery_request(request_text):
            trust_source = "user_request"
        else:
            raise _recovery_block("no_trustworthy_seed")

    normalized_attachments = validate_recovery_attachments_for_use(attachments)
    context_messages: list[dict] = []
    assistant_budget = COMPRESSION_RECOVERY_CONTEXT_MAX_CHARS - len(request_text)

    if trusted_text and assistant_budget > 0:
        bounded = _bounded_recovery_text(trusted_text, assistant_budget)
        if bounded:
            context_messages.append(
                {
                    "role": "assistant",
                    "content": bounded,
                    "_compressed_summary": summary is not None,
                    "_compression_recovery_reference": True,
                }
            )
            assistant_budget -= len(bounded)

    partial = _safe_partial_recovery_text(partial_assistant_text)
    if partial and assistant_budget > 0:
        bounded_partial = _bounded_recovery_text(partial, assistant_budget)
        if bounded_partial:
            context_messages.append(
                {
                    "role": "assistant",
                    "content": bounded_partial,
                    "_compression_recovery_partial": True,
                }
            )

    context_messages.append({"role": "user", "content": request_text})
    session_id = str(getattr(session, "session_id", "") or "")
    parent_id = str(parent_run_id or "")
    fingerprint = _recovery_fingerprint(
        session_id=session_id,
        parent_run_id=parent_id,
        context_messages=context_messages,
        attachments=normalized_attachments,
    )
    return {
        "session_id": session_id,
        "parent_run_id": parent_id,
        "context_messages": context_messages,
        "attachments": normalized_attachments,
        "trust_source": trust_source,
        "fingerprint": fingerprint,
    }
