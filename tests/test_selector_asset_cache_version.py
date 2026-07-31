"""Selector-managed browser asset cache identity contracts."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

import api.updates as updates


VALID_A = "a" * 64
VALID_B = "b" * 64


def _set_selector_mode(monkeypatch, digest: str | None) -> None:
    monkeypatch.setenv("HERMES_WEBUI_LAUNCH_MODE", "selector")
    if digest is None:
        monkeypatch.delenv("HERMES_WEBUI_MANIFEST_SHA256", raising=False)
    else:
        monkeypatch.setenv("HERMES_WEBUI_MANIFEST_SHA256", digest)


def test_selector_asset_version_is_exact_manifest_digest(monkeypatch):
    _set_selector_mode(monkeypatch, VALID_A)

    assert updates._detect_webui_asset_version("unknown") == VALID_A


def test_same_product_version_uses_each_selector_manifest_digest(monkeypatch):
    _set_selector_mode(monkeypatch, VALID_A)
    first = updates._detect_webui_asset_version("v0.51.900")
    _set_selector_mode(monkeypatch, VALID_B)
    second = updates._detect_webui_asset_version("v0.51.900")

    assert first == VALID_A
    assert second == VALID_B
    assert first != second


@pytest.mark.parametrize(
    "digest",
    [
        None,
        "",
        "A" * 64,
        "a" * 63,
        "a" * 65,
        "g" * 64,
    ],
)
def test_selector_mode_fails_closed_on_invalid_manifest_digest(
    monkeypatch, digest
):
    _set_selector_mode(monkeypatch, digest)

    with pytest.raises(RuntimeError, match="selector asset cache identity"):
        updates._detect_webui_asset_version("unknown")


@pytest.mark.parametrize("launch_mode", [None, "", "docker", "Selector"])
def test_non_selector_mode_preserves_product_version(monkeypatch, launch_mode):
    if launch_mode is None:
        monkeypatch.delenv("HERMES_WEBUI_LAUNCH_MODE", raising=False)
    else:
        monkeypatch.setenv("HERMES_WEBUI_LAUNCH_MODE", launch_mode)
    monkeypatch.setenv("HERMES_WEBUI_MANIFEST_SHA256", "not-a-digest")

    assert updates._detect_webui_asset_version("v0.51.900") == "v0.51.900"


def test_exported_asset_version_is_non_empty():
    assert isinstance(updates.WEBUI_ASSET_VERSION, str)
    assert updates.WEBUI_ASSET_VERSION


@pytest.mark.parametrize("digest", [None, "A" * 64])
def test_invalid_selector_asset_identity_fails_during_server_import(
    digest,
):
    env = os.environ.copy()
    env["HERMES_WEBUI_LAUNCH_MODE"] = "selector"
    if digest is None:
        env.pop("HERMES_WEBUI_MANIFEST_SHA256", None)
    else:
        env["HERMES_WEBUI_MANIFEST_SHA256"] = digest

    proc = subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode != 0
    assert "selector asset cache identity is missing or invalid" in proc.stderr
