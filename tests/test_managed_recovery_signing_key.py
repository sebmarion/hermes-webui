from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


def _configure(auth, monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    state_dir.mkdir(mode=0o700)
    state_dir.chmod(0o700)
    monkeypatch.setattr(auth, "STATE_DIR", state_dir)
    monkeypatch.setattr(auth, "_SIGNING_KEY_CACHE", None)


def _write_key(state_dir: Path, raw: bytes, mode: int = 0o600) -> Path:
    key_path = state_dir / ".signing_key"
    key_path.write_bytes(raw)
    key_path.chmod(mode)
    return key_path


def test_managed_recovery_key_separates_durable_persistence_from_cache_load(
    tmp_path, monkeypatch
):
    import api.auth as auth
    import api.atomic_recovery as atomic_recovery

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)

    persistence = auth.strict_persist_signing_key()

    key_path = state_dir / ".signing_key"
    durable_key = key_path.read_bytes()
    assert len(durable_key) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert persistence.key_path == str(key_path)
    assert persistence.durable is True
    assert persistence.created is True
    assert auth._SIGNING_KEY_CACHE is None
    partial = auth.verify_strict_signing_key()
    assert partial.outcome is auth.ManagedSigningKeyVerificationOutcome.PARTIAL
    assert partial.reason == "durable_file_cache_absent"

    cache_receipt = auth.strict_load_signing_key_cache()

    assert cache_receipt.key_path == str(key_path)
    assert cache_receipt.cache_loaded is True
    assert auth._SIGNING_KEY_CACHE == durable_key
    complete = auth.verify_strict_signing_key()
    assert complete.outcome is auth.ManagedSigningKeyVerificationOutcome.PROVED_COMPLETE
    assert complete.reason is None
    assert durable_key.hex() not in repr(persistence)
    assert durable_key.hex() not in repr(cache_receipt)
    with pytest.raises(FrozenInstanceError):
        cache_receipt.cache_loaded = False

    monkeypatch.setattr(auth, "_SIGNING_KEY_CACHE", None)
    combined = atomic_recovery.ensure_managed_internal_recovery_key()
    assert combined.persistence.durable is True
    assert combined.persistence.created is False
    assert combined.cache.cache_loaded is True
    assert auth._SIGNING_KEY_CACHE == durable_key
    assert durable_key.hex() not in repr(combined)


def test_managed_recovery_key_verifier_reports_clean_absence(tmp_path, monkeypatch):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)

    result = auth.verify_strict_signing_key()

    assert result.outcome is auth.ManagedSigningKeyVerificationOutcome.PROVED_ABSENT
    assert result.reason == "durable_file_absent"
    assert result.persistence is None
    assert result.cache is None


@pytest.mark.parametrize(
    ("kind", "reason"),
    (
        ("symlink", "unsafe_durable_file"),
        ("hardlink", "unsafe_durable_file"),
        ("corrupt", "unsafe_durable_file"),
        ("mode", "unsafe_durable_file"),
    ),
)
def test_managed_recovery_key_rejects_unsafe_existing_artifacts(
    tmp_path, monkeypatch, kind, reason
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    outside = tmp_path / "outside"
    outside.write_bytes(b"s" * 32)
    outside.chmod(0o600)
    key_path = state_dir / ".signing_key"
    if kind == "symlink":
        key_path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, key_path)
    elif kind == "corrupt":
        _write_key(state_dir, b"too-short")
    else:
        _write_key(state_dir, b"s" * 32, 0o640)

    with pytest.raises(auth.ManagedSigningKeyError):
        auth.strict_persist_signing_key()

    result = auth.verify_strict_signing_key()
    assert result.outcome is auth.ManagedSigningKeyVerificationOutcome.AMBIGUOUS
    assert result.reason == reason
    assert outside.read_bytes() == b"s" * 32
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600


def test_managed_recovery_key_rejects_non_owner_only_parent(tmp_path, monkeypatch):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    state_dir.chmod(0o750)

    with pytest.raises(auth.ManagedSigningKeyError, match="parent"):
        auth.strict_persist_signing_key()
    result = auth.verify_strict_signing_key()
    assert result.outcome is auth.ManagedSigningKeyVerificationOutcome.AMBIGUOUS


def test_managed_recovery_key_concurrent_creators_adopt_one_winner(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _index: auth.strict_persist_signing_key(), range(24)))

    key_path = state_dir / ".signing_key"
    assert len(key_path.read_bytes()) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert sum(receipt.created for receipt in receipts) == 1
    assert all(receipt.durable for receipt in receipts)
    assert not tuple(state_dir.glob(".signing_key.tmp-*"))


def test_managed_recovery_key_persistence_failure_is_terminal_and_cleans_temp(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)

    def fail_publish(*_args, **_kwargs):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(auth.os, "link", fail_publish)

    with pytest.raises(auth.ManagedSigningKeyError, match="publish"):
        auth.strict_persist_signing_key()
    assert not (state_dir / ".signing_key").exists()
    assert not tuple(state_dir.glob(".signing_key.tmp-*"))
    assert auth._SIGNING_KEY_CACHE is None


def test_managed_recovery_key_keeps_temp_handle_open_through_publication(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    real_open = auth.os.open
    real_fstat = auth.os.fstat
    real_link = auth.os.link
    temp_fd: int | None = None
    open_at_publish = False

    def capture_temp_open(path, flags, *args, **kwargs):
        nonlocal temp_fd
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).startswith(".signing_key.tmp-"):
            temp_fd = fd
        return fd

    def assert_open_then_publish(*args, **kwargs):
        nonlocal open_at_publish
        assert temp_fd is not None
        real_fstat(temp_fd)
        open_at_publish = True
        return real_link(*args, **kwargs)

    monkeypatch.setattr(auth.os, "open", capture_temp_open)
    monkeypatch.setattr(auth.os, "link", assert_open_then_publish)

    receipt = auth.strict_persist_signing_key()

    assert receipt.created is True
    assert open_at_publish is True


def test_managed_recovery_key_detects_existing_file_replacement_race(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    key_path = _write_key(state_dir, b"a" * 32)
    real_read = auth.os.read
    replaced = False

    def replace_after_read(fd, size):
        nonlocal replaced
        raw = real_read(fd, size)
        if not replaced and raw:
            replaced = True
            key_path.unlink()
            _write_key(state_dir, b"b" * 32)
        return raw

    monkeypatch.setattr(auth.os, "read", replace_after_read)

    with pytest.raises(auth.ManagedSigningKeyError, match="identity|changed"):
        auth.strict_persist_signing_key()
    assert key_path.read_bytes() == b"b" * 32


def test_managed_recovery_key_rejects_same_inode_rewrite_during_read(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    key_path = _write_key(state_dir, b"a" * 32)
    real_read = auth.os.read
    rewritten = False

    def rewrite_same_inode_after_read(fd, size):
        nonlocal rewritten
        raw = real_read(fd, size)
        if not rewritten and raw:
            rewritten = True
            with key_path.open("r+b") as stream:
                stream.write(b"b" * 32)
                stream.flush()
                os.fsync(stream.fileno())
        return raw

    monkeypatch.setattr(auth.os, "read", rewrite_same_inode_after_read)

    with pytest.raises(auth.ManagedSigningKeyError, match="changed"):
        auth.strict_load_signing_key_cache()
    assert key_path.read_bytes() == b"b" * 32
    assert auth._SIGNING_KEY_CACHE is None


def test_managed_recovery_key_preserves_cache_until_parent_stability_proved(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    _write_key(state_dir, b"a" * 32)

    def fail_parent_confirmation(*_args, **_kwargs):
        raise auth.ManagedSigningKeyError("synthetic parent stability failure")

    monkeypatch.setattr(auth, "_managed_key_confirm_parent", fail_parent_confirmation)

    with pytest.raises(auth.ManagedSigningKeyError, match="parent stability"):
        auth.strict_load_signing_key_cache()
    assert auth._SIGNING_KEY_CACHE is None


def test_managed_recovery_key_persistence_rechecks_late_quarantine(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    _write_key(state_dir, b"a" * 32)
    real_read_existing = auth._managed_key_read_existing
    reads = 0

    def quarantine_after_final_read(parent_fd):
        nonlocal reads
        raw = real_read_existing(parent_fd)
        reads += 1
        if reads == 2:
            marker = state_dir / ".signing_key.quarantine"
            marker.write_bytes(b"late quarantine")
            marker.chmod(0o600)
        return raw

    monkeypatch.setattr(
        auth,
        "_managed_key_read_existing",
        quarantine_after_final_read,
    )

    with pytest.raises(auth.ManagedSigningKeyError, match="quarantine"):
        auth.strict_persist_signing_key()
    assert reads == 2


def test_managed_recovery_key_cache_rechecks_late_quarantine_before_assignment(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    _write_key(state_dir, b"a" * 32)
    real_read_existing = auth._managed_key_read_existing
    reads = 0

    def quarantine_after_final_read(parent_fd):
        nonlocal reads
        raw = real_read_existing(parent_fd)
        reads += 1
        if reads == 2:
            marker = state_dir / ".signing_key.quarantine"
            marker.write_bytes(b"late quarantine")
            marker.chmod(0o600)
        return raw

    monkeypatch.setattr(
        auth,
        "_managed_key_read_existing",
        quarantine_after_final_read,
    )

    with pytest.raises(auth.ManagedSigningKeyError, match="quarantine"):
        auth.strict_load_signing_key_cache()
    assert reads == 2
    assert auth._SIGNING_KEY_CACHE is None


def test_managed_recovery_key_verifier_rechecks_absence_under_same_parent(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    real_open = auth.os.open
    injected = False

    def create_after_first_absent(path, flags, *args, **kwargs):
        nonlocal injected
        try:
            return real_open(path, flags, *args, **kwargs)
        except FileNotFoundError:
            if path == ".signing_key" and kwargs.get("dir_fd") is not None and not injected:
                injected = True
                _write_key(state_dir, b"a" * 32)
            raise

    monkeypatch.setattr(auth.os, "open", create_after_first_absent)

    result = auth.verify_strict_signing_key()

    assert result.outcome is auth.ManagedSigningKeyVerificationOutcome.AMBIGUOUS
    assert result.reason == "unsafe_durable_file"


def test_managed_recovery_key_quarantines_named_temp_swap_during_publish(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    real_link = auth.os.link

    def swap_named_source_then_publish(source, destination, **kwargs):
        os.unlink(source, dir_fd=kwargs["src_dir_fd"])
        replacement_fd = os.open(
            source,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=kwargs["src_dir_fd"],
        )
        try:
            os.write(replacement_fd, b"b" * 32)
            os.fsync(replacement_fd)
        finally:
            os.close(replacement_fd)
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(auth.os, "link", swap_named_source_then_publish)

    with pytest.raises(auth.ManagedSigningKeyError, match="publication"):
        auth.strict_persist_signing_key()

    assert (state_dir / ".signing_key.quarantine").is_file()
    result = auth.verify_strict_signing_key()
    assert result.outcome is auth.ManagedSigningKeyVerificationOutcome.AMBIGUOUS
    with pytest.raises(auth.ManagedSigningKeyError, match="quarantine"):
        auth.strict_persist_signing_key()


def test_managed_recovery_key_temp_unlink_failure_leaves_durable_quarantine(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    real_unlink = auth.os.unlink
    failed = False

    def fail_first_temp_unlink(path, *args, **kwargs):
        nonlocal failed
        if str(path).startswith(".signing_key.tmp-") and not failed:
            failed = True
            raise OSError("synthetic temp cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(auth.os, "unlink", fail_first_temp_unlink)

    with pytest.raises(auth.ManagedSigningKeyError, match="cleanup"):
        auth.strict_persist_signing_key()

    assert (state_dir / ".signing_key.quarantine").is_file()
    assert auth.verify_strict_signing_key().outcome is (
        auth.ManagedSigningKeyVerificationOutcome.AMBIGUOUS
    )


def test_managed_recovery_key_cache_mismatch_is_ambiguous_and_not_overwritten(
    tmp_path, monkeypatch
):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    _write_key(state_dir, b"a" * 32)
    monkeypatch.setattr(auth, "_SIGNING_KEY_CACHE", b"b" * 32)

    result = auth.verify_strict_signing_key()
    assert result.outcome is auth.ManagedSigningKeyVerificationOutcome.AMBIGUOUS
    assert result.reason == "cache_mismatch"
    with pytest.raises(auth.ManagedSigningKeyError, match="cache"):
        auth.strict_load_signing_key_cache()
    assert auth._SIGNING_KEY_CACHE == b"b" * 32


def test_managed_recovery_key_verifier_is_mutation_free(tmp_path, monkeypatch):
    import api.auth as auth

    state_dir = tmp_path / "state"
    _configure(auth, monkeypatch, state_dir)
    _write_key(state_dir, b"a" * 32)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("verifier attempted mutation")

    monkeypatch.setattr(auth.os, "write", forbidden)
    monkeypatch.setattr(auth.os, "fchmod", forbidden)
    monkeypatch.setattr(auth.os, "link", forbidden)
    monkeypatch.setattr(auth.os, "unlink", forbidden)
    monkeypatch.setattr(auth.os, "fsync", forbidden)

    result = auth.verify_strict_signing_key()

    assert result.outcome is auth.ManagedSigningKeyVerificationOutcome.PARTIAL


def test_managed_recovery_key_snapshots_state_dir_before_creation(tmp_path, monkeypatch):
    import api.auth as auth

    first = tmp_path / "first"
    second = tmp_path / "second"
    _configure(auth, monkeypatch, first)
    second.mkdir(mode=0o700)
    real_token_bytes = auth.secrets.token_bytes

    def switch_state_dir(size):
        monkeypatch.setattr(auth, "STATE_DIR", second)
        return real_token_bytes(size)

    monkeypatch.setattr(auth.secrets, "token_bytes", switch_state_dir)

    receipt = auth.strict_persist_signing_key()

    assert receipt.key_path == str(first / ".signing_key")
    assert (first / ".signing_key").is_file()
    assert not (second / ".signing_key").exists()
