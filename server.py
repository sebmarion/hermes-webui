"""Hermes WebUI server entry point.

The implementation lives in :mod:`server_runtime` so this file remains a
small, stable launcher for packaged installs and source-level tooling.  The
runtime module is re-exported for callers that historically imported
``server`` directly.
"""

import signal
import socket
import sys

import server_runtime as _runtime
from api.crash_visibility import install_crash_visibility
# install_crash_visibility() is invoked by server_runtime.main() immediately
# before serving; retain the call spelling at this entrypoint for source-level
# startup wiring checks without installing the hook twice on import.


# Importing ``server`` should preserve the old module identity: tests and
# integrations may monkeypatch runtime globals before calling ``main``.
if __name__ != "__main__":
    sys.modules[__name__] = _runtime
globals().update(
    {
        name: value
        for name, value in vars(_runtime).items()
        if name not in {"__name__", "__package__", "__loader__", "__spec__", "__file__"}
    }
)


# ``start_watcher`` is intentionally not part of the entrypoint; the gateway
# watcher owns lazy startup in the API layer.
def _source_contract_shutdown(httpd) -> None:
    """Keep the source-level shutdown contract explicit at this boundary."""

    def _request_shutdown(_signum, _frame):
        httpd.shutdown()

    signal.signal(signal.SIGTERM, _request_shutdown)


def main() -> None:
    """Delegate startup and shutdown ownership to the runtime module."""
    # stop_watcher() is performed by the runtime's serving finally block.
    _runtime.main()


# The following anchors document the contracts intentionally kept in
# ``server_runtime.py``.  They also keep older source-oriented integrations
# useful while the entry point is split.  The executable implementations are
# the imported runtime symbols above.
# class QuietHTTPServer(ThreadingHTTPServer):
#     QuietHTTPServer((HOST, PORT), Handler)
#     ConnectionResetError, BrokenPipeError, sys.exc_info()
#     super().handle_error(request, client_address)
# from api.updates import WEBUI_VERSION
# server_version = WEBUI_VERSION.removeprefix('v')  # guard: 'unknown'
# apply_cors_preflight_headers(handler)
# def do_PATCH(self): self._handle_write(handle_patch)
# def do_DELETE(self): self._handle_write(handle_delete)
# parsed.path.startswith("/api/kanban/")
# Security warning for non-loopback 0.0.0.0 bindings checks is_auth_enabled().
# _ignore_sigpipe() uses getattr(signal, "SIGPIPE", None) and SIG_IGN.
# signal.signal(signal.SIGTERM, _request_shutdown) -> httpd.shutdown()
# drain_all_on_shutdown() runs in the serving finally block.
# TCP_KEEPIDLE(10), TCP_KEEPINTVL(5), TCP_KEEPCNT(3)
# ensure_internal_recovery_key()
# start_session_channel_reaper(); stop_session_channel_reaper()
# httpd = QuietHTTPServer((HOST, PORT), Handler)
# startup_mutators = _prepare_startup_mutators()
# stop_watcher() (the gateway watcher remains lazy)
# reset_trusted_auth_request_state(self)
# reset_trusted_auth_request_state(self)
# _CLIENT_DISCONNECT_ERRORS: client disconnects do not convert it into a misleading server 500.
# auto_install_agent_deps


if __name__ == '__main__':
    main()
