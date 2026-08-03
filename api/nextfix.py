"""Local, user-confirmed Safe Change intake for the WebUI report action."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

MAX_REPORT_LEN = 4096


class NextfixUnavailable(RuntimeError):
    """The optional local Safe Change intake is not installed."""


class NextfixValidationError(ValueError):
    """The report payload is invalid or exceeds its bound."""


def _store_factory():
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    source = hermes_home / "safe-change" / "src"
    if not source.is_dir():
        raise NextfixUnavailable("Safe Change intake is not installed on this profile")
    source_text = str(source)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    try:
        from issue_store import IssueStore
    except ImportError as exc:
        raise NextfixUnavailable("Safe Change intake is unavailable") from exc
    return IssueStore


def capture_nextfix(payload: dict[str, Any], store_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Capture one report locally; this endpoint never syncs or promotes it."""
    if not isinstance(payload, dict):
        raise NextfixValidationError("Report must be an object")
    observed = payload.get("observed")
    expected = payload.get("expected")
    if not isinstance(observed, str) or not observed.strip():
        raise NextfixValidationError("Observed problem is required")
    if not isinstance(expected, str) or not expected.strip():
        raise NextfixValidationError("Expected behavior is required")
    observed = observed.strip()
    expected = expected.strip()
    if len(observed) > MAX_REPORT_LEN or len(expected) > MAX_REPORT_LEN:
        raise NextfixValidationError(f"Report fields must be <= {MAX_REPORT_LEN} characters")

    session_id = payload.get("session_id")
    message_index = payload.get("message_index")
    local_ref = None
    if isinstance(session_id, str) and session_id and len(session_id) <= 128:
        if isinstance(message_index, int) and 0 <= message_index <= 1_000_000:
            local_ref = f"webui:{session_id}:{message_index}"

    factory = store_factory or _store_factory
    issue = factory().capture_issue(
        observed=observed,
        expected=expected,
        target_id="fresh-verification",
        surface="interactive-webui",
        local_message_ref=local_ref,
    )
    return {
        "issue_id": issue.issue_id,
        "status": issue.status,
        "message": "Captured locally. Nothing was generated, synced, staged, or applied.",
    }
