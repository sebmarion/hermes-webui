from __future__ import annotations

import os
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest


def _write_private_env(home: Path, text: str) -> Path:
    path = home / ".env"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def _fake_profiles(home: Path, *, profile: str = "alpha"):
    calls: list[tuple[str, object]] = []
    state = SimpleNamespace(
        _active_profile="default",
        _loaded_profile_env_keys=set(),
        _PROTECTED_ENV_KEYS=frozenset({"HERMES_WEBUI_ISOLATED_PROFILE"}),
        _INITIAL_HERMES_HOME="",
        _DEFAULT_HERMES_HOME=home.parent.parent,
        desired_profile=profile,
    )
    state._is_isolated_profile_mode = lambda: False
    state._read_active_profile_file = lambda: state.desired_profile
    state._resolve_profile_home_for_name = lambda name: home.parent / name

    def set_home(value):
        resolved = Path(value)
        calls.append(("set-home", resolved))
        os.environ["HERMES_HOME"] = str(resolved)

    state._set_hermes_home = set_home
    state.install_cron_scheduler_profile_isolation = lambda: calls.append(
        ("install-cron-isolation", None)
    )
    state.calls = calls
    return state


@pytest.fixture
def managed_profile(monkeypatch, tmp_path):
    import api.managed_startup_profile as managed

    managed._reset_managed_startup_profile_for_tests()
    epoch = managed.ProcessEpoch(41001, "test-start-token")
    monkeypatch.setattr(managed, "_current_process_epoch", lambda: epoch)
    monkeypatch.setattr(managed, "_startup_mutations_are_admitted", lambda: True)
    monkeypatch.setattr(managed, "_cached_module_postconditions", lambda _home: ())
    home = tmp_path / "profiles" / "alpha"
    home.mkdir(parents=True)
    home.chmod(0o700)
    profiles = _fake_profiles(home)
    monkeypatch.setattr(managed, "_load_profiles_module", lambda: profiles)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    yield managed, profiles, home, epoch
    for key in set(profiles._loaded_profile_env_keys) | {
        "PROFILE_TOKEN",
        "OLD_PROFILE_TOKEN",
        "HERMES_WEBUI_ISOLATED_PROFILE",
    }:
        os.environ.pop(key, None)
    managed._reset_managed_startup_profile_for_tests()


def test_managed_startup_profile_is_admission_gated(managed_profile, monkeypatch):
    managed, profiles, _home, _epoch = managed_profile
    monkeypatch.setattr(managed, "_startup_mutations_are_admitted", lambda: False)

    with pytest.raises(managed.ManagedStartupProfileAdmissionError):
        managed.apply_managed_startup_profile_state()

    assert profiles.calls == []
    assert (
        managed.verify_managed_startup_profile_state().outcome
        is managed.ManagedStartupProfileVerificationOutcome.PROVED_ABSENT
    )


def test_managed_startup_profile_returns_immutable_typed_receipt_and_replays_once(
    managed_profile,
):
    managed, profiles, home, epoch = managed_profile
    _write_private_env(home, "PROFILE_TOKEN=secret\n")

    first = managed.apply_managed_startup_profile_state()
    second = managed.apply_managed_startup_profile_state()
    verification = managed.verify_managed_startup_profile_state(first)

    assert isinstance(first, managed.ManagedStartupProfileReceipt)
    assert first == second
    assert first.process_epoch == epoch
    assert first.profile_name == "alpha"
    assert first.hermes_home == str(home)
    assert first.hermes_home_device == home.stat().st_dev
    assert first.hermes_home_inode == home.stat().st_ino
    assert first.hermes_home_mode == 0o700
    assert first.env_keys == ("PROFILE_TOKEN",)
    assert profiles.calls == [
        ("set-home", home),
        ("install-cron-isolation", None),
    ]
    assert os.environ["PROFILE_TOKEN"] == "secret"
    assert (
        verification.outcome
        is managed.ManagedStartupProfileVerificationOutcome.PROVED_COMPLETE
    )
    with pytest.raises(FrozenInstanceError):
        first.profile_name = "other"


def test_managed_startup_profile_reports_partial_and_repairs_tamper(
    managed_profile,
):
    managed, profiles, home, _epoch = managed_profile
    receipt = managed.apply_managed_startup_profile_state()
    profiles._active_profile = "tampered"
    os.environ["HERMES_HOME"] = str(home.parent / "tampered")

    partial = managed.verify_managed_startup_profile_state(receipt)
    repaired = managed.apply_managed_startup_profile_state()

    assert (
        partial.outcome
        is managed.ManagedStartupProfileVerificationOutcome.PARTIAL
    )
    assert repaired == receipt
    assert profiles._active_profile == "alpha"
    assert os.environ["HERMES_HOME"] == str(home)
    assert len(profiles.calls) == 4


def test_managed_startup_profile_fails_closed_on_partial_mutation(
    managed_profile,
):
    managed, profiles, _home, _epoch = managed_profile
    profiles._set_hermes_home = lambda _home: None

    with pytest.raises(managed.ManagedStartupProfilePostconditionError):
        managed.apply_managed_startup_profile_state()

    assert (
        managed.verify_managed_startup_profile_state().outcome
        is managed.ManagedStartupProfileVerificationOutcome.PARTIAL
    )


def test_managed_startup_profile_wraps_mutator_failure_after_partial_write(
    managed_profile,
    monkeypatch,
):
    managed, profiles, home, _epoch = managed_profile
    monkeypatch.setattr(
        managed,
        "_cached_module_postconditions",
        lambda _home: (
            (
                "cron.scheduler.profile_isolated",
                ("install-cron-isolation", None) in profiles.calls,
            ),
        ),
    )

    def fail_after_home_write(value):
        os.environ["HERMES_HOME"] = str(value)
        raise RuntimeError("simulated cache patch failure")

    profiles._set_hermes_home = fail_after_home_write

    with pytest.raises(
        managed.ManagedStartupProfilePostconditionError,
        match="profile_postcondition_mismatch",
    ):
        managed.apply_managed_startup_profile_state()

    assert os.environ["HERMES_HOME"] == str(home)
    assert (
        managed.verify_managed_startup_profile_state().outcome
        is managed.ManagedStartupProfileVerificationOutcome.PARTIAL
    )


def test_managed_startup_profile_pins_desired_snapshot_for_process_epoch(
    managed_profile,
):
    managed, profiles, home, _epoch = managed_profile
    first = managed.apply_managed_startup_profile_state()
    profiles.desired_profile = "beta"
    profiles._active_profile = "tampered"

    second = managed.apply_managed_startup_profile_state()

    assert second == first
    assert second.profile_name == "alpha"
    assert profiles._active_profile == "alpha"
    assert os.environ["HERMES_HOME"] == str(home)


def test_managed_startup_profile_rejects_foreign_epoch_and_recaptures_after_fork(
    managed_profile,
    monkeypatch,
):
    managed, profiles, home, first_epoch = managed_profile
    first = managed.apply_managed_startup_profile_state()
    second_epoch = managed.ProcessEpoch(41002, "fork-start-token")
    monkeypatch.setattr(managed, "_current_process_epoch", lambda: second_epoch)
    profiles.desired_profile = "beta"
    (home.parent / "beta").mkdir(mode=0o700)

    foreign = managed.verify_managed_startup_profile_state(first)
    second = managed.apply_managed_startup_profile_state()

    assert foreign.outcome is managed.ManagedStartupProfileVerificationOutcome.AMBIGUOUS
    assert second.process_epoch == second_epoch
    assert second.process_epoch != first_epoch
    assert second.profile_name == "beta"


def test_managed_startup_profile_import_failure_is_ambiguous_and_not_success(
    managed_profile,
    monkeypatch,
):
    managed, _profiles, _home, _epoch = managed_profile

    def unavailable():
        raise ImportError("profiles unavailable")

    monkeypatch.setattr(managed, "_load_profiles_module", unavailable)

    with pytest.raises(managed.ManagedStartupProfileUnavailable):
        managed.apply_managed_startup_profile_state()

    verification = managed.verify_managed_startup_profile_state()
    assert (
        verification.outcome
        is managed.ManagedStartupProfileVerificationOutcome.AMBIGUOUS
    )


def test_managed_startup_profile_removes_prior_keys_and_preserves_protected_env(
    managed_profile,
):
    managed, profiles, home, _epoch = managed_profile
    _write_private_env(home, "PROFILE_TOKEN=new\n")
    profiles._loaded_profile_env_keys = {"OLD_PROFILE_TOKEN"}
    os.environ["OLD_PROFILE_TOKEN"] = "old"
    os.environ["HERMES_WEBUI_ISOLATED_PROFILE"] = "1"

    receipt = managed.apply_managed_startup_profile_state()

    assert "OLD_PROFILE_TOKEN" not in os.environ
    assert os.environ["PROFILE_TOKEN"] == "new"
    assert os.environ["HERMES_WEBUI_ISOLATED_PROFILE"] == "1"
    os.environ["OLD_PROFILE_TOKEN"] = "externally-restored"
    prior_tamper = managed.verify_managed_startup_profile_state(receipt)
    assert prior_tamper.outcome is managed.ManagedStartupProfileVerificationOutcome.PARTIAL
    del os.environ["OLD_PROFILE_TOKEN"]
    os.environ["HERMES_WEBUI_ISOLATED_PROFILE"] = "tampered"
    verification = managed.verify_managed_startup_profile_state(receipt)
    assert (
        verification.outcome
        is managed.ManagedStartupProfileVerificationOutcome.PARTIAL
    )
    with pytest.raises(
        managed.ManagedStartupProfilePostconditionError,
        match="protected environment changed",
    ):
        managed.apply_managed_startup_profile_state()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "mode", "hardlink"])
def test_managed_startup_profile_rejects_unsafe_env_entry(
    managed_profile,
    tmp_path,
    kind,
):
    managed, _profiles, home, _epoch = managed_profile
    env_path = home / ".env"
    if kind == "symlink":
        target = tmp_path / "outside.env"
        target.write_text("PROFILE_TOKEN=secret\n", encoding="utf-8")
        target.chmod(0o600)
        env_path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(env_path, 0o600)
    elif kind == "mode":
        env_path.write_text("PROFILE_TOKEN=secret\n", encoding="utf-8")
        env_path.chmod(0o644)
    else:
        target = home / "linked.env"
        target.write_text("PROFILE_TOKEN=secret\n", encoding="utf-8")
        target.chmod(0o600)
        os.link(target, env_path)

    with pytest.raises(managed.ManagedStartupProfileUnavailable):
        managed.apply_managed_startup_profile_state()


def test_managed_startup_profile_rejects_symlinked_or_public_home(
    managed_profile,
    tmp_path,
):
    managed, profiles, home, _epoch = managed_profile
    home.chmod(0o755)
    with pytest.raises(
        managed.ManagedStartupProfileUnavailable,
        match="owner-private",
    ):
        managed.apply_managed_startup_profile_state()

    managed._reset_managed_startup_profile_for_tests()
    home.rmdir()
    real_home = tmp_path / "private-real-home"
    real_home.mkdir(mode=0o700)
    home.symlink_to(real_home, target_is_directory=True)
    with pytest.raises(managed.ManagedStartupProfileUnavailable):
        managed.apply_managed_startup_profile_state()
    assert profiles.calls == []

    managed._reset_managed_startup_profile_for_tests()
    home.unlink()
    real_base = tmp_path / "real-base"
    real_home = real_base / "profiles" / "alpha"
    real_home.mkdir(parents=True, mode=0o700)
    aliased_base = tmp_path / "base-alias"
    aliased_base.symlink_to(real_base, target_is_directory=True)
    profiles._DEFAULT_HERMES_HOME = aliased_base
    profiles._resolve_profile_home_for_name = (
        lambda name: aliased_base / "profiles" / name
    )
    with pytest.raises(managed.ManagedStartupProfileUnavailable):
        managed.apply_managed_startup_profile_state()


def test_managed_startup_profile_rebind_rejects_home_replacement_before_mutation(
    managed_profile,
    monkeypatch,
):
    managed, profiles, home, _epoch = managed_profile
    admission_checks = 0

    def swap_before_mutation():
        nonlocal admission_checks
        admission_checks += 1
        if admission_checks == 2:
            old_home = home.parent / "alpha.old"
            replacement = home.parent / "replacement"
            replacement.mkdir(mode=0o700)
            home.rename(old_home)
            replacement.rename(home)
        return True

    monkeypatch.setattr(
        managed,
        "_startup_mutations_are_admitted",
        swap_before_mutation,
    )
    with pytest.raises(
        managed.ManagedStartupProfileUnavailable,
        match="identity changed",
    ):
        managed.apply_managed_startup_profile_state()
    assert profiles.calls == []


def test_managed_startup_profile_rejects_detached_ancestor_during_capture(
    managed_profile,
    monkeypatch,
):
    managed, _profiles, home, _epoch = managed_profile
    _write_private_env(home, "PROFILE_TOKEN=secret\n")
    real_open = os.open
    detached = home.parent.with_name("profiles.detached")
    swapped = False

    def detach_after_child_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "alpha" and not swapped:
            swapped = True
            home.parent.rename(detached)
            home.mkdir(parents=True, mode=0o700)
        return descriptor

    monkeypatch.setattr(managed.os, "open", detach_after_child_open)
    with pytest.raises(
        managed.ManagedStartupProfileUnavailable,
        match="component became detached",
    ):
        managed.apply_managed_startup_profile_state()


def test_managed_startup_profile_rejects_noncanonical_double_slash_home(
    managed_profile,
):
    managed, _profiles, home, _epoch = managed_profile
    doubled = "//" + str(home).lstrip("/")
    with pytest.raises(
        managed.ManagedStartupProfileUnavailable,
        match="canonical absolute path",
    ):
        with managed._open_profile_home(doubled):
            pass


def test_managed_startup_profile_detects_env_replacement_between_stat_and_open(
    managed_profile,
    monkeypatch,
):
    managed, _profiles, home, _epoch = managed_profile
    env_path = _write_private_env(home, "PROFILE_TOKEN=first\n")
    staged = home / ".env.next"
    staged.write_text("PROFILE_TOKEN=other\n", encoding="utf-8")
    staged.chmod(0o600)
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == ".env" and not swapped:
            swapped = True
            os.replace(staged, env_path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(managed.os, "open", swapping_open)
    with pytest.raises(
        managed.ManagedStartupProfileUnavailable,
        match="changed while opening",
    ):
        managed.apply_managed_startup_profile_state()


def test_managed_startup_profile_detects_changed_second_read(
    managed_profile,
    monkeypatch,
):
    managed, _profiles, home, _epoch = managed_profile
    env_path = _write_private_env(home, "PROFILE_TOKEN=first\n")
    real_read = managed._read_fd_bounded
    calls = 0

    def changing_read(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            env_path.write_text("PROFILE_TOKEN=other\n", encoding="utf-8")
            env_path.chmod(0o600)
        return real_read(descriptor)

    monkeypatch.setattr(managed, "_read_fd_bounded", changing_read)
    with pytest.raises(
        managed.ManagedStartupProfileUnavailable,
        match="changed while reading",
    ):
        managed.apply_managed_startup_profile_state()


def test_receipt_without_state_is_ambiguous_for_same_and_foreign_epoch(
    managed_profile,
):
    managed, _profiles, _home, epoch = managed_profile
    receipt = managed.apply_managed_startup_profile_state()
    managed._reset_managed_startup_profile_for_tests()

    same = managed.verify_managed_startup_profile_state(receipt)
    foreign_receipt = managed.ManagedStartupProfileReceipt(
        process_epoch=managed.ProcessEpoch(epoch.pid + 1, "foreign"),
        desired_sha256=receipt.desired_sha256,
        profile_name=receipt.profile_name,
        hermes_home=receipt.hermes_home,
        hermes_home_device=receipt.hermes_home_device,
        hermes_home_inode=receipt.hermes_home_inode,
        hermes_home_mode=receipt.hermes_home_mode,
        isolated=receipt.isolated,
        env_keys=receipt.env_keys,
    )
    foreign = managed.verify_managed_startup_profile_state(foreign_receipt)
    absent = managed.verify_managed_startup_profile_state()

    assert same.reason == "managed_profile_receipt_without_state"
    assert foreign.reason == "managed_profile_receipt_from_foreign_epoch"
    assert (
        same.outcome
        is foreign.outcome
        is managed.ManagedStartupProfileVerificationOutcome.AMBIGUOUS
    )
    assert (
        absent.outcome
        is managed.ManagedStartupProfileVerificationOutcome.PROVED_ABSENT
    )


def test_tracking_set_protected_tamper_never_changes_protected_value(
    managed_profile,
):
    managed, profiles, _home, _epoch = managed_profile
    os.environ["HERMES_WEBUI_ISOLATED_PROFILE"] = "1"
    managed.apply_managed_startup_profile_state()
    profiles._loaded_profile_env_keys.add("HERMES_WEBUI_ISOLATED_PROFILE")

    with pytest.raises(
        managed.ManagedStartupProfilePostconditionError,
        match="marked profile-managed",
    ):
        managed.apply_managed_startup_profile_state()

    assert os.environ["HERMES_WEBUI_ISOLATED_PROFILE"] == "1"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_actual_fork_resets_managed_profile_state(managed_profile):
    managed, _profiles, _home, _epoch = managed_profile
    receipt = managed.apply_managed_startup_profile_state()
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(read_fd)
            result = managed.verify_managed_startup_profile_state(receipt)
            os.write(write_fd, f"{result.outcome.value}:{result.reason}".encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 512).decode()
    os.close(read_fd)
    _, status = os.waitpid(child, 0)

    assert status == 0
    assert result == "ambiguous:managed_profile_receipt_without_state"


def test_config_startup_profile_adapter_returns_typed_receipt(
    managed_profile,
    monkeypatch,
):
    import api.config as config

    managed, _profiles, _home, _epoch = managed_profile
    monkeypatch.setattr(config, "_startup_mutations_are_admitted", lambda: True)
    monkeypatch.setattr(
        config,
        "_managed_release_selected_from_environment",
        lambda: True,
    )
    monkeypatch.setattr(managed, "_startup_mutations_are_admitted", lambda: True)

    receipt = config.apply_startup_profile_state()

    assert isinstance(receipt, managed.ManagedStartupProfileReceipt)


def test_config_startup_profile_adapter_fails_closed_when_reconciler_unavailable(
    managed_profile,
    monkeypatch,
):
    import api.config as config

    _managed, _profiles, _home, _epoch = managed_profile
    monkeypatch.setattr(config, "_startup_mutations_are_admitted", lambda: True)
    monkeypatch.setattr(
        config,
        "_managed_release_selected_from_environment",
        lambda: True,
    )
    monkeypatch.setitem(sys.modules, "api.managed_startup_profile", None)

    with pytest.raises(RuntimeError, match="reconciler is unavailable"):
        config.apply_startup_profile_state()


def test_config_startup_profile_adapter_preserves_unmanaged_legacy_init(
    managed_profile,
    monkeypatch,
):
    import api.config as config
    import api.profiles as real_profiles

    _managed, _profiles, _home, _epoch = managed_profile
    calls = []
    monkeypatch.setattr(config, "_startup_mutations_are_admitted", lambda: True)
    monkeypatch.setattr(
        config,
        "_managed_release_selected_from_environment",
        lambda: False,
    )
    monkeypatch.setattr(
        real_profiles,
        "init_profile_state",
        lambda: calls.append("legacy-init"),
    )

    assert config.apply_startup_profile_state() == {"status": "initialized"}
    assert calls == ["legacy-init"]
    with pytest.raises(RuntimeError, match="requires a managed release"):
        config.verify_startup_profile_state()
