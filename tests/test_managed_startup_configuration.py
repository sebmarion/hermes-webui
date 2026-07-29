from __future__ import annotations

import hashlib
import errno
import json
import os
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


@pytest.fixture
def managed_configuration(monkeypatch, tmp_path):
    import api.config as config
    import api.managed_startup_configuration as managed

    managed._reset_managed_startup_configuration_for_tests()
    epoch = managed.ProcessEpoch(51001, "configuration-start-token")
    settings_dir = tmp_path / "state"
    settings_dir.mkdir(mode=0o700)
    journal_dir = tmp_path / "startup-journal"
    journal_dir.mkdir(mode=0o700)
    settings_file = settings_dir / "settings.json"
    monkeypatch.setattr(managed, "_current_process_epoch", lambda: epoch)
    monkeypatch.setattr(managed, "_startup_mutations_are_admitted", lambda: True)
    monkeypatch.setattr(config, "_startup_mutations_are_admitted", lambda: True)
    monkeypatch.setattr(
        config,
        "_managed_release_selected_from_environment",
        lambda: True,
    )
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(
        config,
        "_RUN_ADMISSION_TRANSACTION_ID",
        "configuration-transaction-" + ("x" * 32),
    )
    monkeypatch.setenv("HERMES_WEBUI_MANIFEST_SHA256", "a" * 64)
    monkeypatch.setenv(
        "HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL",
        str(journal_dir / "managed-startup-configuration.json"),
    )
    pending = managed.capture_pending_startup_settings_record(
        settings_file,
        '{"default_workspace":"/managed"}',
        1,
    )
    monkeypatch.setattr(
        config,
        "_DEFERRED_STARTUP_SETTINGS_TEXT",
        pending,
    )
    monkeypatch.setattr(
        config,
        "CLI_TOOLSETS",
        managed.StableCliToolsets(("fenced",)),
    )
    monkeypatch.setattr(
        config,
        "_resolve_cli_toolsets",
        lambda *args, **kwargs: ["terminal", "web"],
    )
    yield managed, config, settings_file, epoch
    managed._reset_managed_startup_configuration_for_tests()


def test_configuration_is_admission_gated_without_mutation(
    managed_configuration,
    monkeypatch,
):
    managed, config, settings_file, _epoch = managed_configuration
    monkeypatch.setattr(managed, "_startup_mutations_are_admitted", lambda: False)

    with pytest.raises(managed.ManagedStartupConfigurationAdmissionError):
        managed.apply_managed_startup_configuration()

    assert not settings_file.exists()
    assert config.CLI_TOOLSETS == ["fenced"]
    assert (
        managed.verify_managed_startup_configuration().outcome
        is managed.ManagedStartupConfigurationVerificationOutcome.PROVED_ABSENT
    )


def test_configuration_rechecks_admission_inside_each_mutator(
    managed_configuration,
    monkeypatch,
):
    managed, config, settings_file, _epoch = managed_configuration
    pending = managed.capture_pending_startup_settings_record(
        settings_file,
        "{}",
        2,
    )
    monkeypatch.setattr(managed, "_startup_mutations_are_admitted", lambda: False)

    with pytest.raises(managed.ManagedStartupConfigurationAdmissionError):
        managed._atomic_write_settings(
            settings_file,
            b"{}",
            pending=pending,
        )
    with pytest.raises(managed.ManagedStartupConfigurationAdmissionError):
        managed._publish_cli_toolsets(config, ("terminal",))

    assert not settings_file.exists()
    assert config.CLI_TOOLSETS == ["fenced"]


def test_configuration_returns_separate_durable_and_process_receipts(
    managed_configuration,
):
    managed, config, settings_file, epoch = managed_configuration

    first = managed.apply_managed_startup_configuration()
    second = managed.apply_managed_startup_configuration()
    verification = managed.verify_managed_startup_configuration(first)

    assert first == second
    assert first.process_epoch == epoch
    assert first.release_binding.transaction_id.startswith(
        "configuration-transaction-"
    )
    assert first.release_binding.manifest_sha256 == "a" * 64
    assert first.settings.status == "proved-complete"
    assert first.settings.sha256 == hashlib.sha256(
        b'{"default_workspace":"/managed"}'
    ).hexdigest()
    assert first.settings.device == settings_file.stat().st_dev
    assert first.settings.inode == settings_file.stat().st_ino
    assert first.settings.mode == 0o600
    assert first.cli.toolsets == ("terminal", "web")
    assert first.cli.publication_id == id(config.CLI_TOOLSETS)
    assert first.cli.sha256
    assert config.CLI_TOOLSETS == ["terminal", "web"]
    assert config._DEFERRED_STARTUP_SETTINGS_TEXT is None
    assert settings_file.read_bytes() == b'{"default_workspace":"/managed"}'
    assert (
        verification.outcome
        is managed.ManagedStartupConfigurationVerificationOutcome.PROVED_COMPLETE
    )
    with pytest.raises(FrozenInstanceError):
        first.cli = None
    with pytest.raises(TypeError):
        config.CLI_TOOLSETS[0] = "tampered"
    journal = settings_file.parent.parent / "startup-journal" / "managed-startup-configuration.json"
    assert journal.exists()
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "complete"


def test_configuration_refuses_foreign_settings_content_without_overwrite(
    managed_configuration,
):
    managed, _config, settings_file, _epoch = managed_configuration
    first = managed.apply_managed_startup_configuration()
    settings_file.write_text('{"tampered":true}', encoding="utf-8")
    settings_file.chmod(0o600)

    partial = managed.verify_managed_startup_configuration(first)
    with pytest.raises(managed.ManagedStartupConfigurationAmbiguous):
        managed.apply_managed_startup_configuration()

    assert (
        partial.outcome
        is managed.ManagedStartupConfigurationVerificationOutcome.PARTIAL
    )
    assert settings_file.read_bytes() == b'{"tampered":true}'


def test_configuration_refuses_even_exact_foreign_file_identity(
    managed_configuration,
):
    managed, _config, settings_file, _epoch = managed_configuration
    first = managed.apply_managed_startup_configuration()
    replacement = settings_file.with_name("replacement.json")
    replacement.write_bytes(b'{"default_workspace":"/managed"}')
    replacement.chmod(0o600)
    os.replace(replacement, settings_file)
    attacker_inode = settings_file.stat().st_ino

    partial = managed.verify_managed_startup_configuration(first)
    with pytest.raises(managed.ManagedStartupConfigurationAmbiguous):
        managed.apply_managed_startup_configuration()

    assert (
        partial.outcome
        is managed.ManagedStartupConfigurationVerificationOutcome.PARTIAL
    )
    assert settings_file.stat().st_ino == attacker_inode


def test_configuration_proves_no_durable_mutation_separately_from_cli(
    managed_configuration,
    monkeypatch,
):
    managed, config, settings_file, _epoch = managed_configuration
    monkeypatch.setattr(config, "_DEFERRED_STARTUP_SETTINGS_TEXT", None)

    receipt = managed.apply_managed_startup_configuration()

    assert receipt.settings.status == "proved-absent"
    assert receipt.settings.sha256 is None
    assert not settings_file.exists()
    assert receipt.cli.toolsets == ("terminal", "web")


def test_configuration_pins_changed_desired_inputs_for_process_epoch(
    managed_configuration,
    monkeypatch,
):
    managed, config, _settings_file, _epoch = managed_configuration
    first = managed.apply_managed_startup_configuration()
    monkeypatch.setattr(
        config,
        "_resolve_cli_toolsets",
        lambda *args, **kwargs: ["newer"],
    )
    config.CLI_TOOLSETS.publish(("tampered",))

    repaired = managed.apply_managed_startup_configuration()

    assert repaired.desired_sha256 == first.desired_sha256
    assert repaired.cli.toolsets == ("terminal", "web")
    assert config.CLI_TOOLSETS == ["terminal", "web"]
    assert config._DEFERRED_STARTUP_SETTINGS_TEXT is None


def test_configuration_rejects_release_binding_reuse(
    managed_configuration,
    monkeypatch,
):
    managed, _config, _settings_file, _epoch = managed_configuration
    managed.apply_managed_startup_configuration()
    monkeypatch.setenv("HERMES_WEBUI_MANIFEST_SHA256", "b" * 64)

    with pytest.raises(
        managed.ManagedStartupConfigurationAmbiguous,
        match="release or journal binding changed",
    ):
        managed.apply_managed_startup_configuration()


def test_configuration_generation_cas_refuses_newer_pending_record(
    managed_configuration,
    monkeypatch,
):
    managed, config, settings_file, _epoch = managed_configuration
    real_write = managed._atomic_write_settings

    def replace_pending_after_write(*args, **kwargs):
        receipt = real_write(*args, **kwargs)
        config._DEFERRED_STARTUP_SETTINGS_TEXT = (
            managed.capture_pending_startup_settings_record(
                settings_file,
                '{"newer":true}',
                2,
            )
        )
        return receipt

    monkeypatch.setattr(
        managed,
        "_atomic_write_settings",
        replace_pending_after_write,
    )
    with pytest.raises(
        managed.ManagedStartupConfigurationAmbiguous,
        match="changed after snapshot",
    ):
        managed.apply_managed_startup_configuration()
    assert config._DEFERRED_STARTUP_SETTINGS_TEXT.generation == 2


def test_complete_configuration_with_newer_pending_generation_is_ambiguous(
    managed_configuration,
):
    managed, config, settings_file, _epoch = managed_configuration
    receipt = managed.apply_managed_startup_configuration()
    config._DEFERRED_STARTUP_SETTINGS_TEXT = (
        managed.capture_pending_startup_settings_record(
            settings_file,
            '{"newer":true}',
            2,
        )
    )

    verification = managed.verify_managed_startup_configuration(receipt)

    assert (
        verification.outcome
        is managed.ManagedStartupConfigurationVerificationOutcome.AMBIGUOUS
    )
    with pytest.raises(managed.ManagedStartupConfigurationAmbiguous):
        managed.apply_managed_startup_configuration()


def test_managed_cli_resolver_failure_is_unavailable_not_fallback(
    managed_configuration,
    monkeypatch,
):
    managed, config, settings_file, _epoch = managed_configuration

    def unavailable(*_args, **_kwargs):
        raise ImportError("hidden discovery unavailable")

    monkeypatch.setattr(config, "_resolve_cli_toolsets", unavailable)
    with pytest.raises(
        managed.ManagedStartupConfigurationUnavailable,
        match="desired state could not be captured",
    ):
        managed.apply_managed_startup_configuration()
    assert not settings_file.exists()


def test_configuration_rejects_symlink_and_public_settings(
    managed_configuration,
    tmp_path,
    monkeypatch,
):
    managed, config, settings_file, _epoch = managed_configuration
    outside = tmp_path / "outside.json"
    outside.write_text('{"outside":true}', encoding="utf-8")
    outside.chmod(0o600)
    settings_file.symlink_to(outside)
    with pytest.raises(managed.ManagedStartupConfigurationUnavailable):
        managed.apply_managed_startup_configuration()
    assert outside.read_text(encoding="utf-8") == '{"outside":true}'

    managed._reset_managed_startup_configuration_for_tests()
    second_journal_dir = tmp_path / "second-startup-journal"
    second_journal_dir.mkdir(mode=0o700)
    monkeypatch.setenv(
        "HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL",
        str(second_journal_dir / "configuration.json"),
    )
    settings_file.unlink()
    settings_file.parent.chmod(0o755)
    settings_file.write_text("{}", encoding="utf-8")
    settings_file.chmod(0o644)
    config._DEFERRED_STARTUP_SETTINGS_TEXT = (
        managed.capture_pending_startup_settings_record(
            settings_file,
            '{"default_workspace":"/managed"}',
            2,
        )
    )
    receipt = managed.apply_managed_startup_configuration()
    assert receipt.settings.migrated_from_mode == 0o644
    assert receipt.settings.parent.mode == 0o755
    assert settings_file.stat().st_mode & 0o777 == 0o600

    managed._reset_managed_startup_configuration_for_tests()
    settings_file.write_text("{}", encoding="utf-8")
    settings_file.chmod(0o664)
    with pytest.raises(managed.ManagedStartupConfigurationUnavailable):
        managed.capture_pending_startup_settings_record(
            settings_file,
            '{"default_workspace":"/managed"}',
            3,
        )


def test_configuration_classifies_replace_crash_retryable_and_preserves_old_file(
    managed_configuration,
    monkeypatch,
):
    managed, config, settings_file, _epoch = managed_configuration
    settings_file.write_text('{"old":true}', encoding="utf-8")
    settings_file.chmod(0o600)
    config._DEFERRED_STARTUP_SETTINGS_TEXT = (
        managed.capture_pending_startup_settings_record(
            settings_file,
            '{"default_workspace":"/managed"}',
            2,
        )
    )
    real_replace = managed.os.replace

    def crash(*args, **kwargs):
        if args[1] == settings_file.name:
            raise OSError(errno.EIO, "simulated replace crash")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(managed.os, "replace", crash)
    with pytest.raises(managed.ManagedStartupConfigurationMutationError) as caught:
        managed.apply_managed_startup_configuration()

    assert (
        caught.value.retry
        is managed.ManagedStartupConfigurationRetry.RETRYABLE
    )
    assert settings_file.read_text(encoding="utf-8") == '{"old":true}'
    assert (
        managed.verify_managed_startup_configuration().outcome
        is managed.ManagedStartupConfigurationVerificationOutcome.PROVED_RETRY_SAFE_PARTIAL
    )
    monkeypatch.setattr(managed.os, "replace", real_replace)
    receipt = managed.apply_managed_startup_configuration()
    assert receipt.settings.status == "proved-complete"


def test_configuration_recovers_parent_fsync_uncertainty_by_exact_reopen(
    managed_configuration,
    monkeypatch,
):
    managed, _config, settings_file, _epoch = managed_configuration
    real_fsync = managed.os.fsync
    failed = False

    def fail_parent_once(descriptor):
        nonlocal failed
        parent = settings_file.parent.stat()
        opened = os.fstat(descriptor)
        if not failed and opened.st_dev == parent.st_dev and opened.st_ino == parent.st_ino:
            failed = True
            raise OSError(errno.EIO, "simulated parent fsync uncertainty")
        return real_fsync(descriptor)

    monkeypatch.setattr(managed.os, "fsync", fail_parent_once)
    with pytest.raises(managed.ManagedStartupConfigurationMutationError):
        managed.apply_managed_startup_configuration()

    assert settings_file.read_bytes() == b'{"default_workspace":"/managed"}'
    assert (
        managed.verify_managed_startup_configuration().outcome
        is managed.ManagedStartupConfigurationVerificationOutcome.PROVED_RETRY_SAFE_PARTIAL
    )
    monkeypatch.setattr(managed.os, "fsync", real_fsync)
    receipt = managed.apply_managed_startup_configuration()
    assert receipt.settings.post.sha256 == receipt.settings.sha256


def test_configuration_retries_exact_legacy_hardening_crash_state(
    managed_configuration,
    monkeypatch,
):
    managed, config, settings_file, _epoch = managed_configuration
    settings_file.write_text('{"old":true}', encoding="utf-8")
    settings_file.chmod(0o644)
    config._DEFERRED_STARTUP_SETTINGS_TEXT = (
        managed.capture_pending_startup_settings_record(
            settings_file,
            '{"default_workspace":"/managed"}',
            2,
        )
    )
    real_replace = managed.os.replace

    def crash(*args, **kwargs):
        if args[1] == settings_file.name:
            raise OSError(errno.EIO, "simulated post-hardening crash")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(managed.os, "replace", crash)

    with pytest.raises(managed.ManagedStartupConfigurationMutationError):
        managed.apply_managed_startup_configuration()
    assert settings_file.stat().st_mode & 0o777 == 0o600
    assert (
        managed.verify_managed_startup_configuration().outcome
        is managed.ManagedStartupConfigurationVerificationOutcome.PROVED_RETRY_SAFE_PARTIAL
    )

    monkeypatch.setattr(managed.os, "replace", real_replace)
    receipt = managed.apply_managed_startup_configuration()
    assert receipt.settings.migrated_from_mode == 0o644


def test_configuration_replays_after_crash_between_settings_and_cli(
    managed_configuration,
    monkeypatch,
):
    managed, config, settings_file, _epoch = managed_configuration
    real_publish = managed._publish_cli_toolsets
    calls = 0

    def crash_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated process publication crash")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(managed, "_publish_cli_toolsets", crash_once)
    with pytest.raises(
        managed.ManagedStartupConfigurationMutationError,
        match="cli publication failed",
    ):
        managed.apply_managed_startup_configuration()

    assert settings_file.read_bytes() == b'{"default_workspace":"/managed"}'
    assert config.CLI_TOOLSETS == ["fenced"]
    receipt = managed.apply_managed_startup_configuration()
    assert receipt.settings.inode == settings_file.stat().st_ino
    assert config.CLI_TOOLSETS == ["terminal", "web"]


def test_configuration_detects_cli_global_identity_tamper_without_rebinding(
    managed_configuration,
):
    managed, config, _settings_file, _epoch = managed_configuration
    first = managed.apply_managed_startup_configuration()
    cached_reader = config.CLI_TOOLSETS
    config.CLI_TOOLSETS = ("tampered",)

    partial = managed.verify_managed_startup_configuration(first)
    with pytest.raises(
        managed.ManagedStartupConfigurationAmbiguous,
        match="identity was replaced",
    ):
        managed.apply_managed_startup_configuration()

    assert (
        partial.outcome
        is managed.ManagedStartupConfigurationVerificationOutcome.PARTIAL
    )
    assert config.CLI_TOOLSETS == ("tampered",)
    assert cached_reader == ["terminal", "web"]


def test_cached_preaccept_cli_alias_observes_atomic_publication(
    managed_configuration,
):
    managed, config, _settings_file, _epoch = managed_configuration
    config.CLI_TOOLSETS = managed.StableCliToolsets(("fenced",))
    cached_reader = config.CLI_TOOLSETS

    receipt = managed.apply_managed_startup_configuration()

    assert config.CLI_TOOLSETS is cached_reader
    assert cached_reader == ["terminal", "web"]
    assert receipt.cli.publication_id == id(cached_reader)
    assert receipt.cli.generation == 1


def test_configuration_receipt_without_state_is_ambiguous(
    managed_configuration,
):
    managed, _config, _settings_file, epoch = managed_configuration
    receipt = managed.apply_managed_startup_configuration()
    managed._reset_managed_startup_configuration_for_tests()

    same = managed.verify_managed_startup_configuration(receipt)
    foreign = managed.ManagedStartupConfigurationReceipt(
        process_epoch=managed.ProcessEpoch(epoch.pid + 1, "foreign"),
        release_binding=receipt.release_binding,
        desired_sha256=receipt.desired_sha256,
        settings=receipt.settings,
        cli=receipt.cli,
    )

    assert same.reason == "configuration_receipt_without_state"
    assert (
        managed.verify_managed_startup_configuration(foreign).reason
        == "configuration_receipt_from_foreign_epoch"
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_configuration_actual_fork_resets_state(managed_configuration):
    managed, _config, _settings_file, _epoch = managed_configuration
    receipt = managed.apply_managed_startup_configuration()
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(read_fd)
            result = managed.verify_managed_startup_configuration(receipt)
            os.write(write_fd, f"{result.outcome.value}:{result.reason}".encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 512).decode()
    os.close(read_fd)
    _, status = os.waitpid(child, 0)

    assert status == 0
    assert result == "ambiguous:configuration_receipt_without_state"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_stable_cli_toolsets_resets_held_lock_after_fork(managed_configuration):
    managed, _config, _settings_file, _epoch = managed_configuration
    publication = managed.StableCliToolsets(("fenced",))
    read_fd, write_fd = os.pipe()
    publication._lock.acquire()
    child = os.fork()
    if child == 0:
        try:
            os.close(read_fd)
            values = publication.snapshot()[0]
            os.write(write_fd, json.dumps(values).encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    publication._lock.release()
    result = os.read(read_fd, 512).decode()
    os.close(read_fd)
    _, status = os.waitpid(child, 0)

    assert status == 0
    assert json.loads(result) == ["fenced"]


@pytest.mark.parametrize(
    "boundary",
    [
        "journal-lock-acquired",
        "journal-temp-fsynced",
        "journal-renamed",
        "journal-parent-fsynced",
        "intent",
        "settings-temp-fsynced",
        "settings-renamed",
        "settings-parent-fsynced",
        "settings-durable",
        "pending-cleared",
        "pending-consumed",
        "cli-published-unrecorded",
        "cli-published",
    ],
)
def test_configuration_hard_crash_reopens_durable_operation_journal(
    tmp_path,
    boundary,
):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(mode=0o700)
    settings = state / "settings.json"
    journal = journal_dir / "configuration-journal.json"
    script = r"""
import json, os, sys
from pathlib import Path
import api.config as config
import api.managed_startup_configuration as managed

state = Path(sys.argv[1])
journal = Path(sys.argv[3])
boundary = sys.argv[2]
settings = state / "settings.json"
config.SETTINGS_FILE = settings
config._RUN_ADMISSION_TRANSACTION_ID = "configuration-transaction-" + ("x" * 32)
config._startup_mutations_are_admitted = lambda: True
config._managed_release_selected_from_environment = lambda: True
config._resolve_cli_toolsets = lambda *args, **kwargs: ["terminal", "web"]
config.CLI_TOOLSETS = managed.StableCliToolsets(("fenced",))
managed._startup_mutations_are_admitted = lambda: True
if not journal.exists():
    config._DEFERRED_STARTUP_SETTINGS_TEXT = managed.capture_pending_startup_settings_record(
        settings, '{"default_workspace":"/managed"}', 1
    )
else:
    config._DEFERRED_STARTUP_SETTINGS_TEXT = None

def crash(phase):
    if phase == boundary:
        os._exit(91)

receipt = managed.apply_managed_startup_configuration(crash_hook=crash)
print(json.dumps({
    "settings": settings.read_text(),
    "cli": list(config.CLI_TOOLSETS),
    "desired": receipt.desired_sha256,
}))
"""
    env = dict(os.environ)
    env["HERMES_WEBUI_MANIFEST_SHA256"] = "a" * 64
    env["HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL"] = str(journal)
    first = subprocess.run(
        [sys.executable, "-c", script, str(state), boundary, str(journal)],
        cwd=str(Path(__file__).parents[1]),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert first.returncode == 91
    if boundary not in {"journal-lock-acquired", "journal-temp-fsynced"}:
        assert journal.exists()
    second = subprocess.run(
        [sys.executable, "-c", script, str(state), "never", str(journal)],
        cwd=str(Path(__file__).parents[1]),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == {
        "settings": '{"default_workspace":"/managed"}',
        "cli": ["terminal", "web"],
        "desired": json.loads(journal.read_text())["desired_sha256"],
    }
    assert json.loads(journal.read_text())["phase"] == "complete"


def test_managed_capture_failure_is_typed_and_fails_closed(
    managed_configuration,
    monkeypatch,
):
    managed, config, _settings_file, _epoch = managed_configuration
    sentinel = managed.PendingStartupSettingsFailure(
        "ManagedStartupConfigurationUnavailable",
        "capture failed",
    )
    config._DEFERRED_STARTUP_SETTINGS_TEXT = sentinel

    with pytest.raises(
        managed.ManagedStartupConfigurationUnavailable,
        match="pending settings capture failed",
    ):
        managed.apply_managed_startup_configuration()


def test_config_records_capture_failure_only_for_managed_selection(
    managed_configuration,
    monkeypatch,
):
    managed, config, _settings_file, _epoch = managed_configuration
    monkeypatch.setattr(
        config,
        "_managed_release_selected_from_environment",
        lambda: True,
    )
    failure = config._managed_pending_settings_failure(OSError("capture failed"))
    assert isinstance(failure, managed.PendingStartupSettingsFailure)
    assert failure.error_type == "OSError"

    monkeypatch.setattr(
        config,
        "_managed_release_selected_from_environment",
        lambda: False,
    )
    assert config._managed_pending_settings_failure(OSError("legacy")) is None


def test_bounded_reader_uses_caller_bound_for_journal(managed_configuration, tmp_path):
    managed, _config, _settings_file, _epoch = managed_configuration
    parent = tmp_path / "large-journal"
    parent.mkdir(mode=0o700)
    candidate = parent / "candidate.json"
    candidate.write_bytes(b"x" * (managed._MAX_SETTINGS_BYTES + 1))
    candidate.chmod(0o600)

    with managed._open_journal_parent(candidate) as descriptor:
        observed = managed._read_bounded_twice(
            descriptor,
            candidate.name,
            max_bytes=managed._MAX_JOURNAL_BYTES,
        )
        assert len(observed[0]) == managed._MAX_SETTINGS_BYTES + 1
        with pytest.raises(managed.ManagedStartupConfigurationUnavailable):
            managed._read_bounded_twice(
                descriptor,
                candidate.name,
                max_bytes=managed._MAX_SETTINGS_BYTES,
            )
    candidate.write_bytes(b"x" * (managed._MAX_JOURNAL_BYTES + 1))
    candidate.chmod(0o600)
    with managed._open_journal_parent(candidate) as descriptor:
        with pytest.raises(managed.ManagedStartupConfigurationUnavailable):
            managed._read_bounded_twice(
                descriptor,
                candidate.name,
                max_bytes=managed._MAX_JOURNAL_BYTES,
            )


def test_structured_retry_evidence_tamper_rejected_even_with_recomputed_digest(
    managed_configuration,
):
    managed, config, _settings_file, _epoch = managed_configuration
    managed.apply_managed_startup_configuration()
    journal = Path(os.environ["HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL"])
    payload = json.loads(journal.read_text())
    payload["planned_postimage"]["inode"] += 1
    unsigned = dict(payload)
    unsigned.pop("evidence_sha256")
    payload["evidence_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    journal.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    )
    journal.chmod(0o600)
    managed._reset_managed_startup_configuration_for_tests()
    config._DEFERRED_STARTUP_SETTINGS_TEXT = None

    with pytest.raises(
        managed.ManagedStartupConfigurationUnavailable,
    ):
        managed.apply_managed_startup_configuration()


def test_journal_reconcile_cleans_only_private_matching_orphan(
    managed_configuration,
):
    managed, _config, _settings_file, _epoch = managed_configuration
    journal = Path(os.environ["HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL"])
    orphan = journal.with_name(f".{journal.name}.deadbeef.tmp")
    unrelated = journal.with_name(".unrelated.tmp")
    orphan.write_bytes(b"partial")
    unrelated.write_bytes(b"keep")
    orphan.chmod(0o600)
    unrelated.chmod(0o600)

    managed.apply_managed_startup_configuration()

    assert not orphan.exists()
    assert unrelated.read_bytes() == b"keep"


def test_journal_reconcile_rejects_matching_symlink_orphan_without_following(
    managed_configuration,
):
    managed, _config, _settings_file, _epoch = managed_configuration
    journal = Path(os.environ["HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL"])
    outside = journal.parent.parent / "outside"
    outside.write_bytes(b"do-not-delete")
    orphan = journal.with_name(f".{journal.name}.hostile.tmp")
    orphan.symlink_to(outside)

    with pytest.raises(
        managed.ManagedStartupConfigurationUnavailable,
        match="journal orphan is unsafe",
    ):
        managed.apply_managed_startup_configuration()

    assert orphan.is_symlink()
    assert outside.read_bytes() == b"do-not-delete"


def test_completed_journal_is_not_reusable_by_a_fresh_transaction(
    managed_configuration,
    monkeypatch,
    tmp_path,
):
    managed, config, settings_file, _epoch = managed_configuration
    managed.apply_managed_startup_configuration()
    original_journal = Path(
        os.environ["HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL"]
    )

    managed._reset_managed_startup_configuration_for_tests()
    config._RUN_ADMISSION_TRANSACTION_ID = "new-transaction-" + ("y" * 32)
    config._DEFERRED_STARTUP_SETTINGS_TEXT = None
    config.CLI_TOOLSETS = managed.StableCliToolsets(("fenced",))
    with pytest.raises(
        managed.ManagedStartupConfigurationAmbiguous,
        match="journal binding changed",
    ):
        managed.apply_managed_startup_configuration()
    assert json.loads(original_journal.read_text())["phase"] == "complete"

    fresh_dir = tmp_path / "fresh-journal"
    fresh_dir.mkdir(mode=0o700)
    fresh_journal = fresh_dir / "managed-startup-configuration.json"
    monkeypatch.setenv(
        "HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL",
        str(fresh_journal),
    )
    managed._reset_managed_startup_configuration_for_tests()
    config._DEFERRED_STARTUP_SETTINGS_TEXT = (
        managed.capture_pending_startup_settings_record(
            settings_file,
            '{"default_workspace":"/next"}',
            2,
        )
    )

    receipt = managed.apply_managed_startup_configuration()

    assert receipt.release_binding.transaction_id.startswith("new-transaction-")
    assert settings_file.read_bytes() == b'{"default_workspace":"/next"}'
    assert json.loads(fresh_journal.read_text())["phase"] == "complete"


def test_journal_lock_path_replacement_after_flock_fails_closed(
    managed_configuration,
):
    managed, _config, _settings_file, _epoch = managed_configuration
    journal = Path(os.environ["HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL"])
    lock = journal.with_name(f".{journal.name}.lock")

    def replace_lock(phase):
        if phase == "journal-lock-acquired":
            lock.unlink()
            lock.write_bytes(b"replacement")
            lock.chmod(0o600)

    with pytest.raises(
        managed.ManagedStartupConfigurationUnavailable,
        match="lock changed after acquisition",
    ):
        managed.apply_managed_startup_configuration(crash_hook=replace_lock)


def test_complete_journal_never_regresses_during_new_process_publication(
    managed_configuration,
    monkeypatch,
):
    managed, config, _settings_file, epoch = managed_configuration
    managed.apply_managed_startup_configuration()
    journal = Path(os.environ["HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL"])
    assert json.loads(journal.read_text())["phase"] == "complete"
    managed._reset_managed_startup_configuration_for_tests()
    config._DEFERRED_STARTUP_SETTINGS_TEXT = None
    config.CLI_TOOLSETS = managed.StableCliToolsets(("fenced",))
    monkeypatch.setattr(
        managed,
        "_current_process_epoch",
        lambda: managed.ProcessEpoch(epoch.pid + 1, "next-process-token"),
    )
    observed = []

    managed.apply_managed_startup_configuration(
        crash_hook=lambda phase: observed.append(
            (phase, json.loads(journal.read_text())["phase"])
        )
        if phase in {"cli-published-unrecorded", "complete"}
        else None
    )

    assert all(phase == "complete" for _hook, phase in observed)
    assert json.loads(journal.read_text())["phase"] == "complete"


def test_config_adapter_fails_closed_when_configuration_reconciler_unavailable(
    managed_configuration,
    monkeypatch,
):
    _managed, config, _settings_file, _epoch = managed_configuration
    monkeypatch.setitem(sys.modules, "api.managed_startup_configuration", None)

    with pytest.raises(RuntimeError, match="configuration reconciler is unavailable"):
        config.apply_deferred_startup_configuration()


def test_config_adapter_preserves_unmanaged_deferred_behavior(
    managed_configuration,
    monkeypatch,
):
    _managed, config, settings_file, _epoch = managed_configuration
    monkeypatch.setattr(
        config,
        "_managed_release_selected_from_environment",
        lambda: False,
    )

    receipt = config.apply_deferred_startup_configuration()

    assert receipt == {
        "settings_rewritten": True,
        "cli_toolsets": ["terminal", "web"],
    }
    assert settings_file.read_text(encoding="utf-8") == (
        '{"default_workspace":"/managed"}'
    )
    with pytest.raises(RuntimeError, match="requires a managed release"):
        config.verify_deferred_startup_configuration()


def test_config_adapter_preserves_unmanaged_legacy_provider_seed(
    managed_configuration,
    monkeypatch,
):
    _managed, config, _settings_file, _epoch = managed_configuration
    calls = []
    monkeypatch.setattr(
        config,
        "_managed_release_selected_from_environment",
        lambda: False,
    )
    monkeypatch.setattr(
        config,
        "_seed_provider_models_from_core",
        lambda: calls.append("legacy-seed"),
    )

    assert config.seed_startup_provider_models() == {"status": "seeded"}
    assert calls == ["legacy-seed"]
    with pytest.raises(RuntimeError, match="requires a managed release"):
        config.verify_startup_provider_models()
