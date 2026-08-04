"""Regression coverage for the bounded transcript render preference.

The stream-end freeze/jump fix (#4328, semantic viewport anchoring) is covered by
test_issue500_message_list_virtualization.py. This file covers the Preferences
toggle and its #4343 contract change:

- Long transcripts now render through the existing bounded window by default.
- The preference remains an explicit opt-out for users who need browser Find to
  cover every historical node.
"""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "static" / "index.html"
PANELS = REPO_ROOT / "static" / "panels.js"
BOOT = REPO_ROOT / "static" / "boot.js"
UI = REPO_ROOT / "static" / "ui.js"
I18N = REPO_ROOT / "static" / "i18n.js"
CONFIG = REPO_ROOT / "api" / "config.py"


def test_virtualize_transcript_setting_is_default_on_and_allowed():
    """Bounded transcript rendering is the default and remains bool-allowlisted."""
    src = CONFIG.read_text(encoding="utf-8")
    assert '"virtualize_transcript": True' in src, "must default ON for bounded transcript DOM"
    assert '"virtualize_transcript",' in src, "must be in _SETTINGS_BOOL_KEYS"
    assert '"virtualize_transcript_optin": False' in src, "opt-in migration marker must exist + default False"
    assert '"virtualize_transcript_optin",' in src, "opt-in marker must be in _SETTINGS_BOOL_KEYS"


def test_settings_preferences_expose_virtualize_toggle():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="settingsVirtualizeTranscript"' in html
    assert 'data-i18n="settings_label_virtualize_transcript"' in html
    assert 'data-i18n="settings_desc_virtualize_transcript"' in html
    assert "Virtualize long transcripts" in html


def test_boot_applies_saved_virtualize_preference_default_on():
    js = BOOT.read_text(encoding="utf-8")
    assert "window._virtualizeTranscript=s.virtualize_transcript!==false" in js
    assert "window._virtualizeTranscript=true" in js


def test_ui_gate_forces_full_render_when_disabled():
    js = UI.read_text(encoding="utf-8")
    start = js.index("function _currentMessageVirtualWindow(")
    body = js[start:start + 900]
    assert "_virtualizeTranscript===false" in body
    assert "virtualized:false" in body


def test_panels_round_trip_and_hot_apply_virtualize_toggle():
    js = PANELS.read_text(encoding="utf-8")
    assert "const virtualizeTranscriptCb=$('settingsVirtualizeTranscript');" in js
    assert "payload.virtualize_transcript=virtualizeTranscriptCb.checked;" in js
    # Keep the legacy marker in the payload for settings-file compatibility.
    assert "payload.virtualize_transcript_optin=virtualizeTranscriptCb.checked;" in js
    assert "virtualizeTranscriptCb.checked=settings.virtualize_transcript!==false;" in js
    assert "window._virtualizeTranscript=virtualizeTranscriptCb.checked;" in js
    # Hot-apply: toggling re-renders the open transcript immediately.
    assert "renderMessages({preserveScroll:true})" in js


def test_virtualize_toggle_i18n_all_locales():
    js = I18N.read_text(encoding="utf-8")
    assert js.count("settings_label_virtualize_transcript:") == 15
    assert js.count("settings_desc_virtualize_transcript:") == 15


# ── #4343 force-off-for-everyone migration (load_settings behavior) ──────────


@pytest.fixture
def _settings_env(tmp_path, monkeypatch):
    """Point load_settings at an isolated settings.json under tmp."""
    import api.config as config

    sf = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", sf)
    return config, sf


def _write(sf, payload):
    sf.write_text(json.dumps(payload), encoding="utf-8")


def test_migration_unset_defaults_on(_settings_env):
    """No stored value inherits the bounded-DOM default."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True})
    assert config.load_settings()["virtualize_transcript"] is True


def test_migration_stored_true_remains_on(_settings_env):
    """An existing true preference remains enabled."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True, "virtualize_transcript": True})
    assert config.load_settings()["virtualize_transcript"] is True


def test_migration_explicit_post_flip_optin_is_honored(_settings_env):
    """An explicit post-flip opt-in (marker present) keeps virtualization on."""
    config, sf = _settings_env
    _write(sf, {
        "onboarding_completed": True,
        "virtualize_transcript": True,
        "virtualize_transcript_optin": True,
    })
    assert config.load_settings()["virtualize_transcript"] is True


def test_migration_optin_marker_without_true_stays_off(_settings_env):
    """Marker present but value false (user opted in then back out) → off."""
    config, sf = _settings_env
    _write(sf, {
        "onboarding_completed": True,
        "virtualize_transcript": False,
        "virtualize_transcript_optin": True,
    })
    assert config.load_settings()["virtualize_transcript"] is False
