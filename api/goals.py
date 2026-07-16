"""WebUI bridge for Hermes persistent session goals."""

from __future__ import annotations

import copy
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:  # Exposed as a module attribute so tests can monkeypatch it directly.
    from hermes_cli.goals import (  # type: ignore
        CONTINUATION_PROMPT_TEMPLATE,
        DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES,
        DEFAULT_MAX_TURNS,
        GoalManager as _NativeGoalManager,
        GoalState,
        judge_goal,
    )
except Exception:  # pragma: no cover - depends on installed hermes-agent
    CONTINUATION_PROMPT_TEMPLATE = ""  # type: ignore
    DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES = 3  # type: ignore
    DEFAULT_MAX_TURNS = 20  # type: ignore
    _NativeGoalManager = None  # type: ignore
    GoalState = None  # type: ignore
    judge_goal = None  # type: ignore

GoalManager = _NativeGoalManager  # type: ignore


class _StaleGoalEvaluation(RuntimeError):
    pass


_DB_CACHE: dict[str, Any] = {}


def _default_max_turns() -> int:
    """Return the configured /goal turn budget, defaulting to Hermes' 20 turns."""
    try:
        from api import config as _config

        cfg = getattr(_config, "cfg", {}) or {}
        goals_cfg = cfg.get("goals", {}) if isinstance(cfg, dict) else {}
        if not isinstance(goals_cfg, dict):
            return int(DEFAULT_MAX_TURNS or 20)
        return max(1, int(goals_cfg.get("max_turns", DEFAULT_MAX_TURNS or 20) or 20))
    except Exception:
        return int(DEFAULT_MAX_TURNS or 20)


def _meta_key(session_id: str) -> str:
    return f"goal:{session_id}"


def _profile_db(profile_home: str | Path):
    """Return a SessionDB pinned to *profile_home*, without reading HERMES_HOME.

    The upstream Hermes GoalManager persists through hermes_cli.goals.load_goal(),
    which resolves SessionDB from process-global HERMES_HOME. WebUI sessions are
    profile-scoped and can run concurrently, so the WebUI bridge uses an explicit
    state.db path whenever the caller provides the session's profile home.
    """
    home = Path(profile_home).expanduser().resolve()
    key = str(home)
    cached = _DB_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from hermes_state import SessionDB  # type: ignore

        db = SessionDB(db_path=home / "state.db")
    except Exception as exc:  # pragma: no cover - import/env dependent
        logger.debug("GoalManager profile DB unavailable for %s: %s", home, exc)
        return None
    _DB_CACHE[key] = db
    return db


class _ProfileGoalManager:
    """Small WebUI-local GoalManager adapter with explicit profile persistence."""

    def __init__(self, session_id: str, *, profile_home: str | Path, default_max_turns: int = 20):
        if GoalState is None:
            raise RuntimeError("Hermes goal state unavailable")
        self.session_id = session_id
        self.profile_home = Path(profile_home).expanduser().resolve()
        self.default_max_turns = int(default_max_turns or DEFAULT_MAX_TURNS or 20)
        self._state = self._load()

    @property
    def state(self):
        return self._state

    def _load(self):
        db = _profile_db(self.profile_home)
        if db is None or not self.session_id:
            return None
        try:
            raw = db.get_meta(_meta_key(self.session_id))
        except Exception as exc:
            logger.debug("GoalManager profile get_meta failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            return GoalState.from_json(raw)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("GoalManager profile state parse failed for %s: %s", self.session_id, exc)
            return None

    def _save(self, state, *, expected_revision: Optional[int] = None) -> None:
        db = _profile_db(self.profile_home)
        if not self.session_id or state is None:
            raise RuntimeError("goal persistence requires session state")
        if db is None:
            self._state = self._load()
            raise RuntimeError("goal state store unavailable")
        try:
            if expected_revision is not None and hasattr(db, "update_meta"):
                accepted = {"value": False}

                def _commit(raw):
                    current = GoalState.from_json(raw) if raw else None  # type: ignore[union-attr]
                    if current is None or getattr(current, "revision", 0) != expected_revision:
                        return raw
                    state.revision = expected_revision + 1
                    accepted["value"] = True
                    return state.to_json()

                db.update_meta(_meta_key(self.session_id), _commit)
                if not accepted["value"]:
                    raise _StaleGoalEvaluation("goal changed while evaluation was running")
            else:
                state.revision = int(getattr(state, "revision", 0) or 0) + 1
                db.set_meta(_meta_key(self.session_id), state.to_json())
        except _StaleGoalEvaluation:
            self._state = self._load()
            raise
        except Exception as exc:
            self._state = self._load()
            logger.warning("GoalManager profile persistence failed: %s", exc)
            raise RuntimeError("goal persistence failed") from exc

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def has_goal(self) -> bool:
        return self._state is not None and self._state.status in ("active", "paused")

    def status_line(self) -> str:
        s = self._state
        if s is None or s.status in ("cleared",):
            return "No active goal. Set one with /goal <text>."
        turns = f"{s.turns_used}/{s.max_turns} turns"
        if s.status == "active":
            return f"⊙ Goal (active, {turns}): {s.goal}"
        if s.status == "paused":
            extra = f" — {s.paused_reason}" if s.paused_reason else ""
            return f"⏸ Goal (paused, {turns}{extra}): {s.goal}"
        if s.status == "done":
            return f"✓ Goal done ({turns}): {s.goal}"
        return f"Goal ({s.status}, {turns}): {s.goal}"

    def set(self, goal: str, *, max_turns: Optional[int] = None):
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("goal text is empty")
        state = GoalState(  # type: ignore[operator]
            goal=goal,
            status="active",
            turns_used=0,
            max_turns=int(max_turns) if max_turns else self.default_max_turns,
            created_at=time.time(),
            last_turn_at=0.0,
        )
        self._state = state
        self._save(state)
        return state

    def pause(self, reason: str = "user-paused"):
        if not self._state:
            return None
        expected_revision = int(getattr(self._state, "revision", 0) or 0)
        self._state.status = "paused"
        self._state.paused_reason = reason
        self._save(self._state, expected_revision=expected_revision)
        return self._state

    def resume(self, *, reset_budget: bool = True):
        if not self._state:
            return None
        expected_revision = int(getattr(self._state, "revision", 0) or 0)
        self._state.status = "active"
        self._state.paused_reason = None
        if reset_budget:
            self._state.turns_used = 0
        self._save(self._state, expected_revision=expected_revision)
        return self._state

    def clear(self) -> None:
        if self._state is None:
            return
        expected_revision = int(getattr(self._state, "revision", 0) or 0)
        self._state.status = "cleared"
        self._save(self._state, expected_revision=expected_revision)
        self._state = None

    def evaluate_after_turn(self, last_response: str, *, user_initiated: bool = True) -> Dict[str, Any]:
        state = self._state
        if state is None or state.status != "active":
            return {
                "status": state.status if state else None,
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "inactive",
                "reason": "no active goal",
                "message": "",
            }

        expected_revision = int(getattr(state, "revision", 0) or 0)
        state.turns_used += 1
        state.last_turn_at = time.time()

        parse_failed = False
        wait_directive = None
        if judge_goal is None:
            verdict, reason = "continue", "goal judge unavailable"
        else:
            try:
                try:
                    judged = judge_goal(
                        state.goal,
                        str(last_response or ""),
                        subgoals=getattr(state, "subgoals", None) or None,
                        contract=(
                            state.contract
                            if callable(getattr(state, "has_contract", None)) and state.has_contract()
                            else None
                        ),
                    )
                except TypeError:
                    judged = judge_goal(state.goal, str(last_response or ""))
                if len(judged) == 4:
                    verdict, reason, parse_failed, wait_directive = judged
                elif len(judged) == 3:
                    verdict, reason, parse_failed = judged
                elif len(judged) == 2:
                    verdict, reason = judged
                else:
                    raise ValueError(f"unsupported goal judge result length: {len(judged)}")
            except Exception as exc:
                # Fail-closed: an evaluation error must not silently pretend the
                # goal is inactive with zero turns used. Log it visibly, mark the
                # goal paused, and stop continuation so the user can inspect.
                logger.warning("goal judge evaluation failed for session=%s: %s", self.session_id, exc)
                state.status = "paused"
                state.paused_reason = f"goal evaluation failed: {type(exc).__name__}"
                self._save(state, expected_revision=expected_revision)
                return {
                    "status": "paused",
                    "should_continue": False,
                    "continuation_prompt": None,
                    "verdict": "error",
                    "reason": state.paused_reason,
                    "message": (
                        f"⏸ Goal paused — evaluation failed ({type(exc).__name__}). "
                        "Use /goal resume to retry, or /goal clear to stop."
                    ),
                }
        state.last_verdict = verdict
        state.last_reason = reason

        # Track consecutive parse failures the same way native GoalManager does.
        if parse_failed:
            state.consecutive_parse_failures = (getattr(state, "consecutive_parse_failures", 0) or 0) + 1
        else:
            state.consecutive_parse_failures = 0

        # Profile-scoped WebUI sessions have no native process/session wake
        # trigger. Pause visibly instead of persisting an active barrier that
        # cannot schedule its own continuation.
        if verdict == "wait":
            state.status = "paused"
            target = "the requested dependency"
            if wait_directive:
                if wait_directive.get("session_id"):
                    target = f"session {wait_directive['session_id']}"
                elif wait_directive.get("pid"):
                    target = f"pid {wait_directive['pid']}"
                elif wait_directive.get("seconds") is not None:
                    target = f"{wait_directive['seconds']}s"
            state.paused_reason = f"goal waiting requires manual resume: {reason}"
            self._save(state, expected_revision=expected_revision)
            return {
                "status": "paused",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "wait",
                "reason": reason,
                "message": (
                    f"⏸ Goal paused — waiting on {target} cannot auto-resume in this WebUI session: {reason}. "
                    "Use /goal resume when the dependency is ready."
                ),
            }

        # Auto-pause after repeated judge parse failures so a bad judge model
        # doesn't burn the whole turn budget silently.
        if (
            parse_failed
            and getattr(state, "consecutive_parse_failures", 0)
            >= DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES
        ):
            state.status = "paused"
            state.paused_reason = (
                f"judge model returned unparseable output {state.consecutive_parse_failures} turns in a row"
            )
            self._save(state, expected_revision=expected_revision)
            return {
                "status": "paused",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "continue",
                "reason": reason,
                "message": (
                    f"⏸ Goal paused — the judge model ({state.consecutive_parse_failures} turns) "
                    "isn't returning the required JSON verdict. "
                    "Use /goal resume to retry, or /goal clear to stop."
                ),
            }

        if verdict == "done":
            state.status = "done"
            self._save(state, expected_revision=expected_revision)
            return {
                "status": "done",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "done",
                "reason": reason,
                "message": f"✓ Goal achieved: {reason}",
            }

        if state.turns_used >= state.max_turns:
            state.status = "paused"
            state.paused_reason = f"turn budget exhausted ({state.turns_used}/{state.max_turns})"
            self._save(state, expected_revision=expected_revision)
            return {
                "status": "paused",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "continue",
                "reason": reason,
                "message": (
                    f"⏸ Goal paused — {state.turns_used}/{state.max_turns} turns used. "
                    "Use /goal resume to keep going, or /goal clear to stop."
                ),
            }

        self._save(state, expected_revision=expected_revision)
        latest = self._load()
        if latest is None or latest.status != "active" or latest.revision != state.revision:
            self._state = latest
            return {
                "status": getattr(latest, "status", None),
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "inactive",
                "reason": "goal changed before continuation dispatch",
                "message": "",
            }
        self._state = latest
        return {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": self.next_continuation_prompt(),
            "goal_revision": latest.revision,
            "verdict": "continue",
            "reason": reason,
            "message": f"↻ Continuing toward goal ({state.turns_used}/{state.max_turns}): {reason}",
        }

    def next_continuation_prompt(self) -> Optional[str]:
        if not self._state or self._state.status != "active":
            return None
        if _NativeGoalManager is not None:
            return _NativeGoalManager.next_continuation_prompt(self)
        return CONTINUATION_PROMPT_TEMPLATE.format(goal=self._state.goal)


def _manager(session_id: str, *, profile_home: str | Path | None = None):
    if GoalManager is None:
        return None
    if profile_home and GoalManager is _NativeGoalManager and GoalState is not None:
        try:
            return _ProfileGoalManager(
                session_id=session_id,
                profile_home=profile_home,
                default_max_turns=_default_max_turns(),
            )
        except Exception as exc:
            logger.debug("Profile-scoped GoalManager unavailable: %s", exc)
            return None
    return GoalManager(session_id=session_id, default_max_turns=_default_max_turns())


def goal_revision_is_active(
    session_id: str,
    revision: Any,
    *,
    profile_home: str | Path | None = None,
) -> bool:
    """Fail-closed execution-time guard for a queued synthetic continuation."""
    try:
        mgr = _manager(session_id, profile_home=profile_home)
        state = getattr(mgr, "state", None) if mgr is not None else None
        return bool(
            state is not None
            and getattr(state, "status", None) == "active"
            and int(getattr(state, "revision", -1)) == int(revision)
        )
    except Exception:
        return False


def _state_payload(state: Any) -> Optional[Dict[str, Any]]:
    if state is None:
        return None
    return {
        "goal": getattr(state, "goal", "") or "",
        "status": getattr(state, "status", "") or "",
        "turns_used": int(getattr(state, "turns_used", 0) or 0),
        "max_turns": int(getattr(state, "max_turns", 0) or 0),
        "last_verdict": getattr(state, "last_verdict", None),
        "last_reason": getattr(state, "last_reason", None),
        "paused_reason": getattr(state, "paused_reason", None),
    }


def _payload(
    *,
    ok: bool = True,
    action: str,
    message: str,
    state: Any = None,
    error: str | None = None,
    kickoff_prompt: str | None = None,
    decision: Dict[str, Any] | None = None,
    message_key: str | None = None,
    message_args: list[Any] | None = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "ok": bool(ok),
        "action": action,
        "message": message,
        "goal": _state_payload(state),
    }
    if error:
        body["error"] = error
    if kickoff_prompt:
        body["kickoff_prompt"] = kickoff_prompt
    if decision is not None:
        body["decision"] = decision
    if message_key:
        body["message_key"] = message_key
    if message_args is not None:
        body["message_args"] = [a for a in message_args if a is not None]
    return body


def _goal_status_payload(state: Any, *, default_message: str | None = None) -> Dict[str, Any]:
    """Build localized-status style payload fields from a goal state."""
    if default_message is None:
        default_message = "No active goal. Set one with /goal <text>."
    if state is None:
        return {"message": default_message, "message_key": "goal_status_none"}
    status = str(getattr(state, "status", "") or "").strip()
    if status in ("cleared",):
        return {"message": default_message, "message_key": "goal_status_none"}
    turns_used = int(getattr(state, "turns_used", 0) or 0)
    max_turns = int(getattr(state, "max_turns", 0) or 0)
    goal = str(getattr(state, "goal", "") or "")
    if status == "active":
        return {
            "message": f"⊙ Goal (active, {turns_used}/{max_turns} turns): {goal}",
            "message_key": "goal_status_active",
            "message_args": [turns_used, max_turns, goal],
        }
    if status == "paused":
        reason = str(getattr(state, "paused_reason", "") or "")
        return {
            "message": f"⏸ Goal (paused, {turns_used}/{max_turns}{' — ' + reason if reason else ''}): {goal}",
            "message_key": "goal_status_paused",
            "message_args": [turns_used, max_turns, reason, goal],
        }
    if status == "done":
        return {
            "message": f"✓ Goal done ({turns_used}/{max_turns}): {goal}",
            "message_key": "goal_status_done",
            "message_args": [turns_used, max_turns, goal],
        }
    return {
        "message": f"Goal ({status}, {turns_used}/{max_turns}): {goal}",
        "message_args": [status, turns_used, max_turns, goal],
    }


def _extract_goal_turns_from_message(message: str) -> tuple[int, int]:
    """Best-effort extraction for continuation messages like '(1/20)'."""
    if not message:
        return 0, 0
    match = re.search(r"\((\d+)\s*/\s*(\d+)\)", message)
    if not match:
        return 0, 0
    try:
        return int(match.group(1)), int(match.group(2))
    except Exception:
        return 0, 0


def _goal_decision_payload(
    decision: Dict[str, Any],
    state: Any,
) -> Dict[str, Any]:
    """Attach goal message i18n key/args to an evaluation decision."""
    if not isinstance(decision, dict):
        return decision
    status = str(decision.get("status") or "").strip()
    reason = str(decision.get("reason") or "").strip()
    turns_used = int(getattr(state, "turns_used", 0) or 0)
    max_turns = int(getattr(state, "max_turns", 0) or 0)
    if (turns_used, max_turns) == (0, 0):
        turns_used, max_turns = _extract_goal_turns_from_message(str(decision.get("message") or ""))

    if status == "done":
        return {
            **decision,
            "message_key": "goal_achieved",
            "message_args": [reason],
        }
    paused_reason = str(getattr(state, "paused_reason", "") or "")
    if status == "paused" and paused_reason.startswith("turn budget exhausted"):
        return {
            **decision,
            "message_key": "goal_paused_budget_exhausted",
            "message_args": [turns_used, max_turns],
        }
    if decision.get("should_continue"):
        return {
            **decision,
            "message_key": "goal_continuing",
            "message_args": [turns_used, max_turns, reason],
        }
    return decision


def goal_state_snapshot(session_id: str, *, profile_home: str | Path | None = None) -> Any:
    """Return a deep copy of current goal state for rollback before kickoff."""
    mgr = _manager(str(session_id or ""), profile_home=profile_home)
    if mgr is None:
        return None
    return copy.deepcopy(getattr(mgr, "state", None))


def restore_goal_state(session_id: str, snapshot: Any, *, profile_home: str | Path | None = None) -> None:
    """Restore a prior goal state after kickoff stream creation fails."""
    mgr = _manager(str(session_id or ""), profile_home=profile_home)
    if mgr is None:
        return
    if snapshot is None:
        try:
            mgr.clear()
        except Exception:
            pass
        return
    if isinstance(mgr, _ProfileGoalManager):
        mgr._state = snapshot
        mgr._save(snapshot)
        return
    try:
        from hermes_cli.goals import save_goal  # type: ignore

        save_goal(str(session_id or ""), snapshot)
    except Exception as exc:  # pragma: no cover - native fallback only
        logger.debug("Goal state restore failed for %s: %s", session_id, exc)


def goal_command_payload(
    session_id: str,
    args: str = "",
    *,
    stream_running: bool = False,
    profile_home: str | Path | None = None,
) -> Dict[str, Any]:
    """Return the WebUI response payload for a /goal command.

    Mirrors the gateway command semantics:
    - /goal or /goal status shows status
    - /goal pause pauses
    - /goal resume resumes without auto-starting a turn
    - /goal clear|stop|done clears
    - /goal <text> sets a new active goal and returns kickoff_prompt so the
      caller can start the first normal user-role turn immediately.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return _payload(ok=False, action="error", error="missing_session", message="session_id required")

    mgr = _manager(sid, profile_home=profile_home)
    if mgr is None:
        return _payload(ok=False, action="error", error="unavailable", message="Goals unavailable on this session.")

    text = str(args or "").strip()
    lower = text.lower()

    if not text or lower == "status":
        state = getattr(mgr, "state", None)
        status_payload = _goal_status_payload(state)
        return _payload(action="status", state=state, **status_payload)

    if lower == "pause":
        state = mgr.pause(reason="user-paused")
        if state is None:
            return _payload(
                ok=False,
                action="pause",
                error="no_goal",
                message="No goal set.",
                message_key="goal_no_goal",
            )
        return _payload(
            action="pause",
            message=f"⏸ Goal paused: {state.goal}",
            message_key="goal_paused",
            message_args=[str(state.goal)],
            state=state,
        )

    if lower == "resume":
        state = mgr.resume()
        if state is None:
            return _payload(
                ok=False,
                action="resume",
                error="no_goal",
                message="No goal to resume.",
                message_key="goal_no_goal",
            )
        return _payload(
            action="resume",
            message=(
                f"▶ Goal resumed: {state.goal}\n"
                "Send a new message, or type continue, to kick it off."
            ),
            message_key="goal_resumed",
            message_args=[str(state.goal)],
            state=state,
        )

    if lower in ("clear", "stop", "done"):
        had = bool(mgr.has_goal())
        mgr.clear()
        return _payload(
            action="clear",
            message="Goal cleared." if had else "No active goal.",
            message_key="goal_cleared" if had else "goal_no_goal",
            state=getattr(mgr, "state", None),
        )

    if stream_running:
        return _payload(
            ok=False,
            action="set",
            error="agent_running",
            message=(
                "Agent is running — use /goal status / pause / clear mid-run, "
                "or /stop before setting a new goal."
            ),
        )

    try:
        state = mgr.set(text)
    except ValueError as exc:
        return _payload(ok=False, action="set", error="invalid_goal", message=f"Invalid goal: {exc}")

    return _payload(
        action="set",
        message=(
            f"⊙ Goal set ({state.max_turns}-turn budget): {state.goal}\n"
            "I'll keep working until the goal is done, you pause/clear it, or the budget is exhausted.\n"
            "Controls: /goal status · /goal pause · /goal resume · /goal clear"
        ),
        message_key="goal_set",
        message_args=[state.max_turns, state.goal],
        state=state,
        kickoff_prompt=state.goal,
    )


def has_active_goal(
    session_id: str,
    *,
    profile_home: str | Path | None = None,
) -> bool:
    """Return True when the session has an active standing goal to evaluate."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    mgr = _manager(sid, profile_home=profile_home)
    if mgr is None:
        return False
    try:
        return bool(mgr.is_active())
    except Exception as exc:
        logger.debug("goal active-state check failed for session=%s: %s", sid, exc)
        return False


def evaluate_goal_after_turn(
    session_id: str,
    last_response: str,
    *,
    user_initiated: bool = True,
    profile_home: str | Path | None = None,
) -> Dict[str, Any]:
    """Evaluate a completed turn against the standing goal, if any."""
    sid = str(session_id or "").strip()
    if not sid:
        return {
            "status": None,
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "inactive",
            "reason": "missing session_id",
            "message": "",
        }
    mgr = _manager(sid, profile_home=profile_home)
    if mgr is None:
        return {
            "status": None,
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "inactive",
            "reason": "goals unavailable",
            "message": "",
        }
    try:
        if not mgr.is_active():
            return {
                "status": getattr(getattr(mgr, "state", None), "status", None),
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "inactive",
                "reason": "no active goal",
                "message": "",
            }
        decision = mgr.evaluate_after_turn(str(last_response or ""), user_initiated=user_initiated)
    except _StaleGoalEvaluation:
        mgr._state = mgr._load() if isinstance(mgr, _ProfileGoalManager) else getattr(mgr, "state", None)
        return {
            "status": getattr(getattr(mgr, "state", None), "status", None),
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "inactive",
            "reason": "goal changed while evaluation was in progress",
            "message": "",
        }
    except Exception as exc:
        logger.warning("goal evaluation failed for session=%s: %s", sid, exc)
        _state = getattr(mgr, "state", None)
        return {
            "status": getattr(_state, "status", None),
            "should_continue": False,
            "continuation_prompt": None,
            "verdict": "error",
            "reason": f"goal evaluation failed: {type(exc).__name__}",
            "message": "⏸ Goal stopped — progress could not be persisted. Retry or resume after storage recovers.",
        }
    if not isinstance(decision, dict):
        decision = {}
    decision.setdefault("should_continue", False)
    decision.setdefault("continuation_prompt", None)
    decision.setdefault("message", "")
    decision = dict(decision)
    decision = _goal_decision_payload(decision, getattr(mgr, "state", None))
    return decision
