#!/usr/bin/env python3
"""Small deny-all ingress gate used by the release-controller integration test."""

from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


READY_PATH = "/__hermes_first_cutover_gate__/ready"


class _GateServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HermesIngressGate/1"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.split("?", 1)[0] != READY_PATH:
            self._json(404, {"error": "not_found"})
            return
        expected = f"Bearer {self.server.controller_token}"
        if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            self._json(401, {"error": "unauthorized"})
            return
        self._json(200, self.server.ready_payload)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.split("?", 1)[0] == "/api/chat/start":
            self._json(503, {"error": "ingress_fenced"})
            return
        self._json(503, {"error": "ingress_fenced"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--controller-token-file", required=True)
    parser.add_argument("--ready-receipt", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    token_path = Path(args.controller_token_file)
    ready_path = Path(args.ready_receipt)
    token = token_path.read_text(encoding="ascii").strip()
    if not token:
        raise SystemExit("controller token is empty")

    server = _GateServer((args.host, args.port), _Handler)
    server.controller_token = token
    server.ready_payload = {
        "schema_version": 1,
        "ready": True,
        "mode": "deny-all-no-proxy",
        "pid": os.getpid(),
        "process_start": time.time(),
        "process_start_token": str(time.time_ns()),
        "instance_id": secrets.token_hex(16),
        "host": args.host,
        "port": args.port,
        "controller_endpoint": READY_PATH,
    }
    ready_path.write_text(
        json.dumps(server.ready_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(ready_path, 0o600)

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
        try:
            ready_path.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
