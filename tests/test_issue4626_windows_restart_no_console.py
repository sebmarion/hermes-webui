"""Regression tests for supervised WebUI restart behavior on Windows.

Only bootstrap.py may spawn a Windows replacement. The update API must stage
changes and require explicit externally supervised restart approval.

Before #4626, both used python.exe (console subsystem) without CREATE_NO_WINDOW, so
every restart flashed an empty terminal window on Windows. If the user closed that
window it took the WebUI down with it.

These are source-level assertions because the behavior is Windows-only (the
DETACHED_PROCESS / CREATE_NO_WINDOW subprocess constants and pythonw.exe only exist
on win32), so the spawn path can't be exercised on the Linux CI box. We pin:
  - bootstrap adds CREATE_NO_WINDOW and prefers pythonw.exe
  - api/updates._schedule_restart never spawns or exits the serving process
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
UPDATES_PY = (REPO / "api" / "updates.py").read_text(encoding="utf-8")
BOOTSTRAP_PY = (REPO / "bootstrap.py").read_text(encoding="utf-8")


class TestWindowsRestartConsoleSuppression:
    def test_updates_restart_requires_external_coordinator(self):
        restart_body = UPDATES_PY.split("def _schedule_restart", 1)[1].split(
            "def _ensure_gateway_restart", 1
        )[0]
        assert "return False" in restart_body
        assert "threading.Thread" not in restart_body
        assert "subprocess.Popen" not in restart_body
        assert "os.execv" not in restart_body
        assert "os._exit" not in restart_body

    def test_bootstrap_restart_adds_create_no_window(self):
        assert "CREATE_NO_WINDOW" in BOOTSTRAP_PY, (
            "bootstrap.py Windows restart must add CREATE_NO_WINDOW to the Popen "
            "creationflags so the supervisor auto-restart does not flash a console (#4626)"
        )

    def test_bootstrap_restart_prefers_pythonw(self):
        assert "w.exe" in BOOTSTRAP_PY, (
            "bootstrap.py should prefer pythonw.exe over python.exe on Windows (#4626)"
        )

    def test_bootstrap_uses_defensive_getattr_for_flags(self):
        # The flags must be resolved with getattr(subprocess, <attr>, 0) so the
        # win32-only constants can't AttributeError if the branch is reached under
        # a non-Windows interpreter (e.g. a win32-simulating test).
        assert 'getattr(subprocess, _attr, 0)' in BOOTSTRAP_PY, (
            "bootstrap.py must resolve win32-only creationflags defensively via "
            "getattr(subprocess, attr, 0) (#4626)"
        )

    def test_bootstrap_windows_restart_preserves_server_logs(self):
        # The windowless Windows child (pythonw + CREATE_NO_WINDOW) has no console,
        # so its stdout/stderr must go to a real log file — NOT DEVNULL, which would
        # silently drop all server diagnostics after a supervisor restart. Pin that
        # the win32 foreground branch redirects to the bootstrap log sink.
        win_branch = BOOTSTRAP_PY.split('if sys.platform == "win32":', 1)[-1].split("os.execv", 1)[0]
        assert "bootstrap-" in win_branch and ".log" in win_branch, (
            "bootstrap.py win32 restart must redirect the windowless child's stdout "
            "to a real log file (state_dir/bootstrap-<port>.log), not DEVNULL (#4626)"
        )
        assert "stdout=subprocess.DEVNULL" not in win_branch, (
            "bootstrap.py win32 restart must NOT send the windowless child's stdout to "
            "DEVNULL — server diagnostics would be lost with no console (#4626)"
        )
        assert "stderr=subprocess.STDOUT" in win_branch, (
            "bootstrap.py win32 restart should fold stderr into the stdout log sink (#4626)"
        )

    def test_windows_restart_changes_are_win32_scoped(self):
        # Both edits live under a sys.platform == 'win32' guard so there is no
        # Linux/macOS behavior change.
        assert 'sys.platform == "win32"' in BOOTSTRAP_PY or "sys.platform == 'win32'" in BOOTSTRAP_PY, (
            "bootstrap.py restart change must stay inside the win32 branch"
        )
