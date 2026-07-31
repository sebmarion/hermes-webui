from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from urllib.parse import quote

import api
import api.config as api_config
import api.routes as routes

ROOT = Path(__file__).resolve().parent.parent
ASSET_DIGEST = "c" * 64


def _patch_asset_version(monkeypatch) -> None:
    updates = importlib.import_module("api.updates")
    monkeypatch.setattr(updates, "WEBUI_ASSET_VERSION", ASSET_DIGEST)
    monkeypatch.setattr(api, "updates", updates)


class _FakeHandler:
    def __init__(self, request_headers=None):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.headers = dict(request_headers or {})
        self.wfile = self

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None


def _get(path, request_headers=None):
    handler = _FakeHandler(request_headers)
    routes.handle_get(handler, SimpleNamespace(path=path, query=""))
    return handler


def test_config_owner_returns_checkout_static_root():
    static_root = ROOT / "static"
    assert api_config.get_static_root() == static_root
    assert api_config.get_index_html_path() == static_root / "index.html"


def test_manifest_routes_follow_selected_static_root(tmp_path, monkeypatch):
    static_root = tmp_path / "static"
    static_root.mkdir()
    manifest_path = static_root / "manifest.json"
    payload = json.dumps({"name": "temp", "display": "standalone"}).encode("utf-8")
    manifest_path.write_bytes(payload)
    monkeypatch.setattr(api_config, "get_static_root", lambda: static_root)

    handler = _get("/manifest.json")
    assert handler.status == 200
    assert handler.header("Content-Type") == "application/manifest+json; charset=utf-8"
    assert handler.header("Cache-Control") == "no-store"
    assert bytes(handler.body) == payload

    session_handler = _get("/session/manifest.webmanifest")
    assert session_handler.status == 200
    assert bytes(session_handler.body) == payload


def test_service_worker_and_favicon_follow_selected_static_root(tmp_path, monkeypatch):
    static_root = tmp_path / "static"
    static_root.mkdir()
    sw_path = static_root / "sw.js"
    sw_path.write_text("const version = '__WEBUI_VERSION__';\n", encoding="utf-8")
    favicon_path = static_root / "favicon.ico"
    favicon_path.write_bytes(b"favicon-bytes")
    monkeypatch.setattr(api_config, "get_static_root", lambda: static_root)
    _patch_asset_version(monkeypatch)

    sw_handler = _get("/sw.js")
    expected = sw_path.read_text(encoding="utf-8").replace(
        "__WEBUI_VERSION__", quote(ASSET_DIGEST, safe="")
    ).encode("utf-8")
    assert sw_handler.status == 200
    assert sw_handler.header("Service-Worker-Allowed") == "/"
    assert sw_handler.header("Cache-Control") == "no-store"
    assert bytes(sw_handler.body) == expected

    favicon_handler = _get("/favicon.ico")
    assert favicon_handler.status == 200
    assert favicon_handler.header("Content-Type") == "image/x-icon"
    assert bytes(favicon_handler.body) == b"favicon-bytes"

    favicon_path.unlink()
    missing_favicon_handler = _get("/favicon.ico")
    assert missing_favicon_handler.status == 204


def test_index_shell_and_static_route_use_selected_root(tmp_path, monkeypatch):
    static_root = tmp_path / "static"
    static_root.mkdir()

    index_path = static_root / "index.html"
    index_path.write_bytes(
        b"<html>__WEBUI_VERSION__ __MAX_UPLOAD_BYTES__ __CSRF_TOKEN_JSON__ temp</html>"
    )
    ui_path = static_root / "ui.js"
    ui_path.write_bytes(b"console.log('temp static');\n")

    monkeypatch.setattr(api_config, "get_static_root", lambda: static_root)
    monkeypatch.setattr(api_config, "get_index_html_path", lambda: index_path)
    _patch_asset_version(monkeypatch)
    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})
    monkeypatch.setattr(routes, "_STATIC_CACHE", {})

    shell = routes._render_index_shell_base()
    assert "temp" in shell
    assert ASSET_DIGEST in shell
    assert "__WEBUI_VERSION__" not in shell
    assert "__MAX_UPLOAD_BYTES__" not in shell
    assert "__CSRF_TOKEN_JSON__" in shell

    temp_static = _get("/static/ui.js")
    assert temp_static.status == 200
    assert bytes(temp_static.body) == b"console.log('temp static');\n"
    assert bytes(temp_static.body) != (ROOT / "static" / "ui.js").read_bytes()

    traversal = _get("/static/../api/routes.py")
    assert traversal.status == 404


def test_real_app_shell_uses_exact_asset_cache_version(monkeypatch):
    _patch_asset_version(monkeypatch)
    monkeypatch.setattr(
        api_config, "get_index_html_path", lambda: ROOT / "static" / "index.html"
    )
    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})

    shell = routes._render_index_shell_base()
    query_versions = re.findall(r"[?&]v=([^\"'&<>\s]+)", shell)

    assert query_versions
    assert set(query_versions) == {ASSET_DIGEST}
    assert (
        f"window.__HERMES_WEBUI_BUNDLE_VERSION__='{ASSET_DIGEST}';" in shell
    )
    assert "v=unknown" not in shell
    assert "__WEBUI_VERSION__" not in shell


def test_login_and_service_worker_use_exact_asset_cache_version(monkeypatch):
    _patch_asset_version(monkeypatch)
    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})

    login = _get("/login")
    login_body = bytes(login.body).decode("utf-8")
    assert login.status == 200
    assert f'static/login.js?v={ASSET_DIGEST}' in login_body
    assert "v=unknown" not in login_body
    assert "__WEBUI_VERSION__" not in login_body

    service_worker = _get("/sw.js")
    sw_body = bytes(service_worker.body).decode("utf-8")
    assert service_worker.status == 200
    assert f"const CACHE_NAME = 'hermes-shell-{ASSET_DIGEST}';" in sw_body
    assert f"const VQ = '?v={ASSET_DIGEST}';" in sw_body
    shell_assets = sw_body.split("const SHELL_ASSETS = [", 1)[1].split("];", 1)[0]
    versioned_paths = re.findall(r"'([^']+)'\s*\+\s*VQ", shell_assets)
    resolved_versioned_paths = [
        f"{path}?v={ASSET_DIGEST}" for path in versioned_paths
    ]
    assert versioned_paths
    assert all(
        path.rsplit("?v=", 1)[1] == ASSET_DIGEST
        for path in resolved_versioned_paths
    )
    assert shell_assets.count("+ VQ") == len(versioned_paths)
    assert "hermes-shell-unknown" not in sw_body
    assert "?v=unknown" not in sw_body
    assert "__WEBUI_VERSION__" not in sw_body
