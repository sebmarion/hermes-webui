"""Regression tests for the /background task tracker.

Covers two bugs caught in review of PR #932:

1. `get_results()` was calling `_BACKGROUND_TASKS.pop(parent_sid, [])`, which
   removed EVERY task (including still-running ones) on the first poll. Once
   popped, `complete_background()` could no longer find the task to mark done,
   so the final answer was silently lost.

2. The `_handle_background` worker thread called `_run_agent_streaming` but
   never invoked `complete_background()` after it returned. With no completion
   hook, every background task stayed in `status="running"` forever —
   `get_results()` filtered them out of its "done" list, and the user never
   saw the result.

These two bugs together made the `/background` command completely
non-functional as originally shipped.  The fix in api/background.py +
api/routes.py wires the completion hook and keeps running tasks in the
tracker until they resolve.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


# Ensure the repo root is importable without relying on CWD.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class TestGetResultsKeepsRunningTasks(unittest.TestCase):
    """get_results() MUST NOT drop still-running tasks from _BACKGROUND_TASKS."""

    def setUp(self):
        import api.background as bg
        self._state_tmp = tempfile.TemporaryDirectory()
        self._original_tasks_file = getattr(bg, "_BACKGROUND_TASKS_FILE", None)
        bg._BACKGROUND_TASKS_FILE = pathlib.Path(self._state_tmp.name) / "tasks.json"
        bg._BACKGROUND_TASKS_LOADED = True
        bg._BACKGROUND_TASKS.clear()
        self.bg = bg

    def tearDown(self):
        self.bg._BACKGROUND_TASKS.clear()
        self.bg._BACKGROUND_TASKS_LOADED = True
        if self._original_tasks_file is not None:
            self.bg._BACKGROUND_TASKS_FILE = self._original_tasks_file
        self._state_tmp.cleanup()

    def test_running_tasks_survive_get_results_call(self):
        """A running task must remain in the tracker so complete_background()
        can still find it after the first poll returns."""
        parent = "parent-session-1"
        self.bg.track_background(
            parent_sid=parent, bg_sid="bg-a", stream_id="s-a",
            task_id="task-a", prompt="long task",
        )

        # First poll: task is still running, no done results to return
        results = self.bg.get_results(parent)
        self.assertEqual(results, [], "no done tasks yet — nothing to return")

        # The running task MUST still be tracked — otherwise the worker
        # thread's complete_background call cannot find it.
        remaining = self.bg.get_background_tasks(parent)
        self.assertEqual(len(remaining), 1, (
            "get_results dropped the still-running task — subsequent "
            "complete_background() calls will silently no-op and the "
            "result will be lost forever"
        ))
        self.assertEqual(remaining[0]["status"], "running")
        self.assertEqual(remaining[0]["task_id"], "task-a")

    def test_done_tasks_are_returned_and_removed(self):
        """Done tasks are returned and popped; running tasks stay."""
        parent = "parent-session-2"
        self.bg.track_background(parent, "bg-done", "s-d", "task-done", "p1")
        self.bg.track_background(parent, "bg-run", "s-r", "task-run", "p2")
        self.bg.complete_background(parent, "task-done", "42")

        results = self.bg.get_results(parent)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_id"], "task-done")
        self.assertEqual(results[0]["answer"], "42")

        # Done one is gone; running one is still tracked
        remaining = self.bg.get_background_tasks(parent)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["task_id"], "task-run")
        self.assertEqual(remaining[0]["status"], "running")

    def test_complete_after_poll_still_reaches_tracker(self):
        """Regression for the original bug: poll → complete → poll must surface
        the result.  Before the fix, the first poll popped the running task and
        complete_background()'s loop iterated over an empty list."""
        parent = "parent-session-3"
        self.bg.track_background(parent, "bg-x", "s-x", "task-x", "slow task")

        # Frontend polls before the task finishes
        first = self.bg.get_results(parent)
        self.assertEqual(first, [])

        # Worker thread finishes and calls complete_background
        self.bg.complete_background(parent, "task-x", "answer!")

        # Next poll must surface the answer
        second = self.bg.get_results(parent)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["task_id"], "task-x")
        self.assertEqual(second[0]["answer"], "answer!")

    def test_empty_parent_is_cleaned_up(self):
        """When all tasks are done and returned, the parent key is removed from the dict."""
        parent = "parent-session-4"
        self.bg.track_background(parent, "bg-1", "s-1", "task-1", "p")
        self.bg.complete_background(parent, "task-1", "ok")
        self.bg.get_results(parent)
        self.assertNotIn(parent, self.bg._BACKGROUND_TASKS)

    def test_completed_result_survives_process_state_reload(self):
        parent = "parent-restart"
        self.bg.track_background(parent, "bg-r", "s-r", "task-r", "persist me")
        self.bg.complete_background(parent, "task-r", "durable answer")

        self.bg._BACKGROUND_TASKS.clear()
        self.bg._BACKGROUND_TASKS_LOADED = False

        self.assertEqual(
            self.bg.get_results(parent),
            [
                {
                    "task_id": "task-r",
                    "prompt": "persist me",
                    "answer": "durable answer",
                    "completed_at": unittest.mock.ANY,
                }
            ],
        )

    def test_running_task_becomes_visible_interruption_after_restart(self):
        parent = "parent-interrupted"
        self.bg.track_background(parent, "bg-i", "s-i", "task-i", "long task")

        self.bg._BACKGROUND_TASKS.clear()
        self.bg._BACKGROUND_TASKS_LOADED = False

        results = self.bg.get_results(parent)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_id"], "task-i")
        self.assertIn("restarted", results[0]["answer"].lower())


class TestBackgroundCompletionHookWiring(unittest.TestCase):
    """Static check: the _handle_background worker thread must call
    complete_background() after _run_agent_streaming returns.  Without this,
    running tasks stay forever-running and the user never sees the result.
    """

    def test_run_bg_and_notify_calls_complete_background(self):
        """_handle_background must wrap _run_agent_streaming in a function
        that subsequently invokes complete_background(parent_sid, task_id, answer)."""
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        # Locate the _handle_background function
        idx = routes_src.find("def _handle_background(")
        self.assertGreater(idx, -1, "_handle_background() not found in routes.py")
        # Take a generous window around the function body
        end = routes_src.find("\ndef ", idx + 1)
        body = routes_src[idx:end if end > 0 else idx + 3000]

        self.assertIn("complete_background", body, (
            "_handle_background worker must call complete_background() after "
            "_run_agent_streaming returns — otherwise the tracker never "
            "transitions the task to status='done' and /api/background/status "
            "returns nothing forever. See api/background.py:complete_background."
        ))
        # Must extract the last assistant message content from the bg session
        self.assertIn("_run_agent_streaming", body)
        self.assertIn("Session.load", body, (
            "_run_bg_and_notify must reload the bg session to extract the "
            "final assistant reply so complete_background gets an actual answer"
        ))
        self.assertIn("background_finalizer", body)
        self.assertIn("unregister_active_run(finalizer_run_id)", body)


if __name__ == "__main__":
    unittest.main()
