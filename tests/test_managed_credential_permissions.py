from __future__ import annotations

import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


SENSITIVE_NAMES = (
    ".env",
    "google_token.json",
    "google_client_secret.json",
    ".signing_key",
    "auth.json",
)


def _configure(monkeypatch: pytest.MonkeyPatch, home: Path, mode: str | None = None) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_SKIP_CHMOD", raising=False)
    if mode is None:
        monkeypatch.delenv("HERMES_HOME_MODE", raising=False)
    else:
        monkeypatch.setenv("HERMES_HOME_MODE", mode)


def _write_sensitive(home: Path, name: str, mode: int) -> Path:
    path = home / name
    path.write_text("not-a-real-secret", encoding="utf-8")
    path.chmod(mode)
    return path


def test_managed_fix_repairs_every_existing_sensitive_file_and_returns_frozen_receipt(
    tmp_path, monkeypatch
):
    from api.startup import (
        ManagedCredentialPermissionStatus,
        strict_fix_credential_permissions,
    )

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    paths = [_write_sensitive(home, name, 0o666) for name in SENSITIVE_NAMES]
    _configure(monkeypatch, home)

    receipt = strict_fix_credential_permissions()

    assert receipt.status is ManagedCredentialPermissionStatus.COMPLETE
    assert receipt.hermes_home == str(home)
    assert receipt.policy_mode == 0o600
    assert receipt.inventory == SENSITIVE_NAMES
    assert receipt.existing == SENSITIVE_NAMES
    assert receipt.changed == SENSITIVE_NAMES
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
    with pytest.raises(FrozenInstanceError):
        receipt.status = ManagedCredentialPermissionStatus.SKIPPED


def test_managed_fix_partial_inventory_repairs_only_existing_files(tmp_path, monkeypatch):
    from api.startup import strict_fix_credential_permissions

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    auth = _write_sensitive(home, "auth.json", 0o640)
    env = _write_sensitive(home, ".env", 0o600)
    _configure(monkeypatch, home)

    receipt = strict_fix_credential_permissions()

    assert receipt.existing == (".env", "auth.json")
    assert receipt.changed == ("auth.json",)
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_managed_fix_explicit_skip_is_terminal_and_does_not_require_home(tmp_path, monkeypatch):
    from api.startup import (
        ManagedCredentialPermissionStatus,
        strict_fix_credential_permissions,
        verify_strict_credential_permissions,
    )

    missing = tmp_path / "missing"
    monkeypatch.setenv("HERMES_HOME", str(missing))
    monkeypatch.setenv("HERMES_SKIP_CHMOD", "true")
    monkeypatch.setenv("HERMES_HOME_MODE", "not-octal")

    receipt = strict_fix_credential_permissions()
    verification = verify_strict_credential_permissions()

    assert receipt.status is ManagedCredentialPermissionStatus.SKIPPED
    assert receipt.hermes_home == str(missing)
    assert receipt.inventory == SENSITIVE_NAMES
    assert receipt.existing == ()
    assert receipt.changed == ()
    assert verification.outcome.value == "proved-complete"
    assert verification.receipt == receipt


def test_managed_declared_group_mode_preserves_group_bits_but_removes_world_bits(
    tmp_path, monkeypatch
):
    from api.startup import strict_fix_credential_permissions

    home = tmp_path / "hermes"
    home.mkdir(mode=0o750)
    path = _write_sensitive(home, "auth.json", 0o664)
    _configure(monkeypatch, home, "750")

    receipt = strict_fix_credential_permissions()

    assert receipt.policy_mode == 0o750
    assert receipt.changed == ("auth.json",)
    assert stat.S_IMODE(path.stat().st_mode) == 0o660


@pytest.mark.parametrize(
    "raw_mode",
    [
        "garbage",
        "888",
        "-1",
        "10000",
        "+600",
        "0_600",
        "00_00",
        "0o600",
        "60",
        "00000",
    ],
)
def test_managed_malformed_declared_mode_fails_strict(tmp_path, monkeypatch, raw_mode):
    from api.startup import ManagedCredentialPermissionError, strict_fix_credential_permissions

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    _configure(monkeypatch, home, raw_mode)

    with pytest.raises(ManagedCredentialPermissionError, match="HERMES_HOME_MODE"):
        strict_fix_credential_permissions()


@pytest.mark.parametrize("raw_mode", ["600", "0600", "750", "0750", "000", "0000"])
def test_managed_canonical_declared_modes_are_accepted(tmp_path, monkeypatch, raw_mode):
    from api.startup import strict_fix_credential_permissions

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    _write_sensitive(home, "auth.json", 0o660)
    _configure(monkeypatch, home, raw_mode)

    receipt = strict_fix_credential_permissions()

    assert receipt.policy_mode == int(raw_mode, 8)


def test_managed_fix_closes_parent_fd_when_parent_fstat_fails(tmp_path, monkeypatch):
    import api.startup as startup

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    _configure(monkeypatch, home)
    real_open = startup.os.open
    real_fstat = startup.os.fstat
    real_close = startup.os.close
    parent_fds: list[int] = []
    closed_fds: list[int] = []

    def track_parent_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == str(home) and kwargs.get("dir_fd") is None:
            parent_fds.append(fd)
        return fd

    def fail_parent_fstat(fd):
        if fd in parent_fds:
            raise OSError("synthetic parent fstat failure")
        return real_fstat(fd)

    def track_close(fd):
        closed_fds.append(fd)
        return real_close(fd)

    monkeypatch.setattr(startup.os, "open", track_parent_open)
    monkeypatch.setattr(startup.os, "fstat", fail_parent_fstat)
    monkeypatch.setattr(startup.os, "close", track_close)

    with pytest.raises(startup.ManagedCredentialPermissionError):
        startup.strict_fix_credential_permissions()

    assert len(parent_fds) == 1
    assert parent_fds[0] in closed_fds


def test_managed_verifier_classifies_safe_loose_and_unsafe_inventory(tmp_path, monkeypatch):
    from api.startup import (
        ManagedCredentialVerificationOutcome,
        verify_strict_credential_permissions,
    )

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    path = _write_sensitive(home, "auth.json", 0o600)
    _configure(monkeypatch, home)

    complete = verify_strict_credential_permissions()
    assert complete.outcome is ManagedCredentialVerificationOutcome.PROVED_COMPLETE
    assert complete.receipt is not None
    assert complete.reason is None

    path.chmod(0o640)
    absent = verify_strict_credential_permissions()
    assert absent.outcome is ManagedCredentialVerificationOutcome.PROVED_ABSENT
    assert absent.receipt is None
    assert absent.reason == "repairable_permissions"

    path.unlink()
    path.symlink_to(home / ".env")
    ambiguous = verify_strict_credential_permissions()
    assert ambiguous.outcome is ManagedCredentialVerificationOutcome.AMBIGUOUS
    assert ambiguous.receipt is None
    assert ambiguous.reason == "unsafe_inventory"


def test_managed_fix_rejects_symlink_and_hardlink_without_mutating_target(
    tmp_path, monkeypatch
):
    from api.startup import ManagedCredentialPermissionError, strict_fix_credential_permissions

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("outside", encoding="utf-8")
    target.chmod(0o644)
    (home / "auth.json").symlink_to(target)
    _configure(monkeypatch, home)

    with pytest.raises(ManagedCredentialPermissionError):
        strict_fix_credential_permissions()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644

    (home / "auth.json").unlink()
    os.link(target, home / "auth.json")
    with pytest.raises(ManagedCredentialPermissionError, match="link count"):
        strict_fix_credential_permissions()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_managed_fix_rejects_wrong_owner(tmp_path, monkeypatch):
    import api.startup as startup

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    _write_sensitive(home, "auth.json", 0o600)
    _configure(monkeypatch, home)
    real_fstat = startup.os.fstat

    def wrong_owner_for_regular(fd):
        value = real_fstat(fd)
        if stat.S_ISREG(value.st_mode):
            fields = list(value)
            fields[4] = value.st_uid + 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(startup.os, "fstat", wrong_owner_for_regular)

    with pytest.raises(startup.ManagedCredentialPermissionError, match="owner"):
        startup.strict_fix_credential_permissions()


def test_managed_fix_fails_on_chmod_error(tmp_path, monkeypatch):
    import api.startup as startup

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    path = _write_sensitive(home, "auth.json", 0o644)
    _configure(monkeypatch, home)

    def fail_fchmod(_fd, _mode):
        raise OSError("synthetic chmod failure")

    monkeypatch.setattr(startup.os, "fchmod", fail_fchmod)

    with pytest.raises(startup.ManagedCredentialPermissionError, match="chmod"):
        startup.strict_fix_credential_permissions()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_managed_fix_detects_post_chmod_directory_entry_replacement(tmp_path, monkeypatch):
    import api.startup as startup

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    path = _write_sensitive(home, "auth.json", 0o644)
    _configure(monkeypatch, home)
    real_fchmod = startup.os.fchmod

    def replace_after_fchmod(fd, mode):
        real_fchmod(fd, mode)
        path.unlink()
        path.write_text("replacement", encoding="utf-8")
        path.chmod(0o644)

    monkeypatch.setattr(startup.os, "fchmod", replace_after_fchmod)

    with pytest.raises(startup.ManagedCredentialPermissionError, match="identity"):
        startup.strict_fix_credential_permissions()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_managed_fix_rejects_inventory_that_changes_during_scan(tmp_path, monkeypatch):
    import api.startup as startup

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    _write_sensitive(home, "auth.json", 0o600)
    _configure(monkeypatch, home)
    real_open = startup.os.open
    injected = False

    def create_after_observed_missing(path, flags, *args, **kwargs):
        nonlocal injected
        try:
            return real_open(path, flags, *args, **kwargs)
        except FileNotFoundError:
            if path == ".env" and kwargs.get("dir_fd") is not None and not injected:
                injected = True
                _write_sensitive(home, ".env", 0o600)
            raise

    monkeypatch.setattr(startup.os, "open", create_after_observed_missing)

    with pytest.raises(startup.ManagedCredentialPermissionError, match="inventory"):
        startup.strict_fix_credential_permissions()


def test_managed_verifier_classifies_stat_failure_as_ambiguous(tmp_path, monkeypatch):
    import api.startup as startup

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    _write_sensitive(home, "auth.json", 0o600)
    _configure(monkeypatch, home)
    real_fstat = startup.os.fstat

    def fail_for_regular(fd):
        value = real_fstat(fd)
        if stat.S_ISREG(value.st_mode):
            raise OSError("synthetic stat failure")
        return value

    monkeypatch.setattr(startup.os, "fstat", fail_for_regular)

    result = startup.verify_strict_credential_permissions()

    assert result.outcome is startup.ManagedCredentialVerificationOutcome.AMBIGUOUS
    assert result.receipt is None
    assert result.reason == "unsafe_inventory"


def test_managed_api_fails_closed_without_nofollow_support(tmp_path, monkeypatch):
    import api.startup as startup

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    _write_sensitive(home, "auth.json", 0o600)
    _configure(monkeypatch, home)
    monkeypatch.delattr(startup.os, "O_NOFOLLOW")

    with pytest.raises(startup.ManagedCredentialPermissionError, match="O_NOFOLLOW"):
        startup.strict_fix_credential_permissions()
    result = startup.verify_strict_credential_permissions()
    assert result.outcome is startup.ManagedCredentialVerificationOutcome.AMBIGUOUS


def test_managed_verifier_never_calls_fchmod(tmp_path, monkeypatch):
    import api.startup as startup

    home = tmp_path / "hermes"
    home.mkdir(mode=0o700)
    _write_sensitive(home, "auth.json", 0o640)
    _configure(monkeypatch, home)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("verifier mutated permissions")

    monkeypatch.setattr(startup.os, "fchmod", forbidden)
    result = startup.verify_strict_credential_permissions()

    assert result.outcome is startup.ManagedCredentialVerificationOutcome.PROVED_ABSENT
