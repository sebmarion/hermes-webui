"""Hermes Web UI -- startup helpers."""
from __future__ import annotations
import errno
import os, stat, subprocess, sys
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_HAS_OPEN_DIR_FD = os.open in os.supports_dir_fd
_HAS_STAT_DIR_FD = os.stat in os.supports_dir_fd
_HAS_STAT_NOFOLLOW = os.stat in os.supports_follow_symlinks
_MANAGED_HOME_MODE_RE = re.compile(r"(?:[0-7]{3}|0[0-7]{3})\Z")

# Credential files that should never be world-readable
_SENSITIVE_FILES = (
    '.env',
    'google_token.json',
    'google_client_secret.json',
    '.signing_key',
    'auth.json',
)


class ManagedCredentialPermissionError(RuntimeError):
    """A managed credential-permission operation could not be proven safe."""


class ManagedCredentialPermissionStatus(str, Enum):
    COMPLETE = "complete"
    SKIPPED = "skipped"


class ManagedCredentialVerificationOutcome(str, Enum):
    PROVED_COMPLETE = "proved-complete"
    PROVED_ABSENT = "proved-absent"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ManagedCredentialPermissionReceipt:
    """Non-secret evidence emitted by the strict managed permission operation."""

    status: ManagedCredentialPermissionStatus
    hermes_home: str
    policy_mode: int | None
    inventory: tuple[str, ...]
    existing: tuple[str, ...]
    changed: tuple[str, ...]


@dataclass(frozen=True)
class ManagedCredentialPermissionVerification:
    """Reconciler-friendly result for a mutation-free inventory verification."""

    outcome: ManagedCredentialVerificationOutcome
    receipt: ManagedCredentialPermissionReceipt | None
    reason: str | None


@dataclass(frozen=True)
class _ManagedCredentialPolicy:
    hermes_home: str
    skip: bool
    declared_mode: int | None

    @property
    def receipt_mode(self) -> int | None:
        if self.skip:
            return None
        return self.declared_mode if self.declared_mode is not None else 0o600


@dataclass(frozen=True)
class _ManagedFileEvidence:
    device: int
    inode: int
    mode: int
    owner: int
    link_count: int


def _managed_credential_policy() -> _ManagedCredentialPolicy:
    raw_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    hermes_home = os.path.abspath(os.path.expanduser(raw_home))
    skip = os.environ.get("HERMES_SKIP_CHMOD", "").strip().lower() in ("1", "true")
    if skip:
        return _ManagedCredentialPolicy(hermes_home, True, None)

    raw_mode = os.environ.get("HERMES_HOME_MODE", "")
    if not raw_mode:
        return _ManagedCredentialPolicy(hermes_home, False, None)
    if _MANAGED_HOME_MODE_RE.fullmatch(raw_mode) is None:
        raise ManagedCredentialPermissionError(
            "HERMES_HOME_MODE must be exactly three octal digits or four "
            "octal digits with one leading zero"
        )
    declared_mode = int(raw_mode, 8)
    return _ManagedCredentialPolicy(hermes_home, False, declared_mode)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_managed_parent(parent_stat: os.stat_result) -> None:
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise ManagedCredentialPermissionError("HERMES_HOME is not a directory")
    if hasattr(os, "getuid") and parent_stat.st_uid != os.getuid():
        raise ManagedCredentialPermissionError("HERMES_HOME has the wrong owner")


def _validate_managed_file(file_stat: os.stat_result, name: str) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise ManagedCredentialPermissionError(f"{name} is not a regular file")
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise ManagedCredentialPermissionError(f"{name} has the wrong owner")
    if file_stat.st_nlink != 1:
        raise ManagedCredentialPermissionError(f"{name} has an unsafe link count")


def _open_managed_parent(policy: _ManagedCredentialPolicy) -> tuple[int, os.stat_result]:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ManagedCredentialPermissionError(
            "strict credential verification requires O_NOFOLLOW support"
        )
    if (
        not _HAS_OPEN_DIR_FD
        or not _HAS_STAT_DIR_FD
        or not _HAS_STAT_NOFOLLOW
    ):
        raise ManagedCredentialPermissionError(
            "strict credential verification requires dir_fd and no-follow stat support"
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    parent_fd: int | None = None
    try:
        path_before = os.stat(policy.hermes_home, follow_symlinks=False)
        parent_fd = os.open(policy.hermes_home, flags)
        try:
            parent_stat = os.fstat(parent_fd)
        except BaseException:
            os.close(parent_fd)
            parent_fd = None
            raise
    except OSError as exc:
        raise ManagedCredentialPermissionError(
            "could not open and inspect HERMES_HOME safely"
        ) from exc
    try:
        _validate_managed_parent(path_before)
        _validate_managed_parent(parent_stat)
        if not _same_inode(path_before, parent_stat):
            raise ManagedCredentialPermissionError("HERMES_HOME identity changed while opening")
        return parent_fd, parent_stat
    except BaseException:
        os.close(parent_fd)
        raise


def _confirm_parent_identity(
    policy: _ManagedCredentialPolicy,
    parent_stat: os.stat_result,
) -> None:
    try:
        path_after = os.stat(policy.hermes_home, follow_symlinks=False)
    except OSError as exc:
        raise ManagedCredentialPermissionError(
            "HERMES_HOME identity could not be rechecked"
        ) from exc
    _validate_managed_parent(path_after)
    if not _same_inode(parent_stat, path_after):
        raise ManagedCredentialPermissionError("HERMES_HOME identity changed during inspection")


def _managed_file_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _open_managed_file(parent_fd: int, name: str) -> int | None:
    try:
        return os.open(name, _managed_file_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise ManagedCredentialPermissionError(
            f"{name} could not be opened safely"
        ) from exc


def _entry_stat(parent_fd: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise ManagedCredentialPermissionError(
            f"{name} identity could not be verified"
        ) from exc


def _file_evidence(value: os.stat_result) -> _ManagedFileEvidence:
    return _ManagedFileEvidence(
        device=value.st_dev,
        inode=value.st_ino,
        mode=stat.S_IMODE(value.st_mode),
        owner=value.st_uid,
        link_count=value.st_nlink,
    )


def _confirm_managed_inventory(
    parent_fd: int,
    observed: dict[str, _ManagedFileEvidence | None],
) -> None:
    for name in _SENSITIVE_FILES:
        expected = observed[name]
        file_fd = _open_managed_file(parent_fd, name)
        if file_fd is None:
            if expected is not None:
                raise ManagedCredentialPermissionError(
                    f"{name} inventory changed during inspection"
                )
            continue
        try:
            current = os.fstat(file_fd)
            entry = _entry_stat(parent_fd, name)
            if expected is None:
                raise ManagedCredentialPermissionError(
                    f"{name} inventory changed during inspection"
                )
            if not _same_inode(current, entry) or _file_evidence(current) != expected:
                raise ManagedCredentialPermissionError(
                    f"{name} inventory identity changed during inspection"
                )
            _validate_managed_file(current, name)
            _validate_managed_file(entry, name)
        finally:
            os.close(file_fd)


def _managed_inventory(
    policy: _ManagedCredentialPolicy,
    *,
    mutate: bool,
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    parent_fd, parent_stat = _open_managed_parent(policy)
    existing: list[str] = []
    changed: list[str] = []
    repairable = False
    observed: dict[str, _ManagedFileEvidence | None] = {}
    try:
        for name in _SENSITIVE_FILES:
            file_fd = _open_managed_file(parent_fd, name)
            if file_fd is None:
                observed[name] = None
                continue
            try:
                before = os.fstat(file_fd)
                entry_before = _entry_stat(parent_fd, name)
                _validate_managed_file(before, name)
                _validate_managed_file(entry_before, name)
                if not _same_inode(before, entry_before):
                    raise ManagedCredentialPermissionError(
                        f"{name} identity changed while opening"
                    )
                existing.append(name)
                current = stat.S_IMODE(before.st_mode)
                forbidden = current & (0o007 if policy.declared_mode is not None else 0o077)
                if not forbidden:
                    observed[name] = _file_evidence(before)
                    continue
                repairable = True
                if not mutate:
                    observed[name] = _file_evidence(before)
                    continue
                target = current & ~0o007 if policy.declared_mode is not None else 0o600
                try:
                    os.fchmod(file_fd, target)
                except OSError as exc:
                    raise ManagedCredentialPermissionError(
                        f"{name} chmod failed"
                    ) from exc
                after = os.fstat(file_fd)
                entry_after = _entry_stat(parent_fd, name)
                if not _same_inode(before, after) or not _same_inode(after, entry_after):
                    raise ManagedCredentialPermissionError(
                        f"{name} identity changed during chmod"
                    )
                _validate_managed_file(after, name)
                _validate_managed_file(entry_after, name)
                if stat.S_IMODE(after.st_mode) != target:
                    raise ManagedCredentialPermissionError(
                        f"{name} chmod could not be verified"
                    )
                changed.append(name)
                observed[name] = _file_evidence(after)
            except OSError as exc:
                raise ManagedCredentialPermissionError(
                    f"{name} could not be inspected safely"
                ) from exc
            finally:
                os.close(file_fd)
        _confirm_managed_inventory(parent_fd, observed)
        _confirm_parent_identity(policy, parent_stat)
        return tuple(existing), tuple(changed), repairable
    finally:
        os.close(parent_fd)


def _skipped_managed_receipt(
    policy: _ManagedCredentialPolicy,
) -> ManagedCredentialPermissionReceipt:
    return ManagedCredentialPermissionReceipt(
        status=ManagedCredentialPermissionStatus.SKIPPED,
        hermes_home=policy.hermes_home,
        policy_mode=None,
        inventory=_SENSITIVE_FILES,
        existing=(),
        changed=(),
    )


def strict_fix_credential_permissions() -> ManagedCredentialPermissionReceipt:
    """Strictly enforce the managed credential-file permission policy.

    Unlike :func:`fix_credential_permissions`, this API never suppresses an
    uncertain result. It mutates only an already-opened, verified inode.
    """

    policy = _managed_credential_policy()
    if policy.skip:
        return _skipped_managed_receipt(policy)
    existing, changed, _repairable = _managed_inventory(policy, mutate=True)
    return ManagedCredentialPermissionReceipt(
        status=ManagedCredentialPermissionStatus.COMPLETE,
        hermes_home=policy.hermes_home,
        policy_mode=policy.receipt_mode,
        inventory=_SENSITIVE_FILES,
        existing=existing,
        changed=changed,
    )


def verify_strict_credential_permissions() -> ManagedCredentialPermissionVerification:
    """Verify managed credential permissions without mutating filesystem state."""

    try:
        policy = _managed_credential_policy()
        if policy.skip:
            receipt = _skipped_managed_receipt(policy)
            return ManagedCredentialPermissionVerification(
                ManagedCredentialVerificationOutcome.PROVED_COMPLETE,
                receipt,
                None,
            )
        existing, _changed, repairable = _managed_inventory(policy, mutate=False)
        if repairable:
            return ManagedCredentialPermissionVerification(
                ManagedCredentialVerificationOutcome.PROVED_ABSENT,
                None,
                "repairable_permissions",
            )
        receipt = ManagedCredentialPermissionReceipt(
            status=ManagedCredentialPermissionStatus.COMPLETE,
            hermes_home=policy.hermes_home,
            policy_mode=policy.receipt_mode,
            inventory=_SENSITIVE_FILES,
            existing=existing,
            changed=(),
        )
        return ManagedCredentialPermissionVerification(
            ManagedCredentialVerificationOutcome.PROVED_COMPLETE,
            receipt,
            None,
        )
    except ManagedCredentialPermissionError:
        return ManagedCredentialPermissionVerification(
            ManagedCredentialVerificationOutcome.AMBIGUOUS,
            None,
            "unsafe_inventory",
        )


def fix_credential_permissions() -> None:
    """Ensure sensitive files in HERMES_HOME have safe permissions.

    Respects:
      - HERMES_SKIP_CHMOD=1  → bypass entirely
      - HERMES_HOME_MODE     → group bits are allowed if set by the operator,
                               only world-readable/world-writable files are fixed
    """
    if os.environ.get('HERMES_SKIP_CHMOD', '').strip() in ('1', 'true'):
        return

    # Parse operator-declared mode to know if group bits are intentional
    declared_mode = None
    raw_mode = os.environ.get('HERMES_HOME_MODE', '').strip()
    if raw_mode:
        try:
            declared_mode = int(raw_mode, 8)
        except ValueError:
            pass

    hermes_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    if not hermes_home.is_dir():
        return
    for name in _SENSITIVE_FILES:
        fpath = hermes_home / name
        if not fpath.exists():
            continue
        try:
            current = stat.S_IMODE(fpath.stat().st_mode)
            # If operator declared a mode, allow group bits but still fix world bits
            if declared_mode is not None:
                if current & 0o007:  # other bits set (world-readable/writable)
                    fpath.chmod(current & ~0o007)
                    print(f'  [security] removed world bits on {fpath.name} ({oct(current)} -> {oct(current & ~0o007)})', flush=True)
            else:
                if current & 0o077:  # group or other bits set
                    fpath.chmod(0o600)
                    print(f'  [security] fixed permissions on {fpath.name} ({oct(current)} -> 0600)', flush=True)
        except OSError:
            pass  # best-effort; don't abort startup


def _agent_dir() -> Path | None:
    hermes_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    for raw in [os.environ.get('HERMES_WEBUI_AGENT_DIR', '').strip(), str(hermes_home / 'hermes-agent')]:
        if not raw:
            continue
        p = Path(raw).expanduser()
        if p.is_dir():
            return p.resolve()
    return None

def _trusted_agent_dir(agent_dir: Path) -> bool:
    """Return True if agent_dir passes ownership and permission checks.

    Validates that the directory is not world- or group-writable and,
    on POSIX systems, is owned by the current process user.

    Intentionally does NOT enforce a canonical path (i.e. does not require
    the dir to be ~/.hermes/hermes-agent), so custom HERMES_WEBUI_AGENT_DIR
    paths work correctly when HERMES_WEBUI_AUTO_INSTALL=1 is set.
    """
    try:
        st = agent_dir.stat()
        if stat.S_IMODE(st.st_mode) & 0o022:
            # World- or group-writable — untrusted
            return False
        if hasattr(os, 'getuid') and st.st_uid != os.getuid():
            # Not owned by current user (POSIX only; Windows fallback skips)
            return False
        return True
    except OSError:
        return False


def auto_install_agent_deps() -> bool:
    if any(
        os.environ.get(key) is not None
        for key in (
            'HERMES_WEBUI_RELEASE_ROOT',
            'HERMES_WEBUI_RELEASE_PATH',
            'HERMES_WEBUI_MANIFEST_SHA256',
            'HERMES_WEBUI_LAUNCH_MODE',
        )
    ):
        print('[!!] Auto-install disabled for managed release.', flush=True)
        return False
    enabled = os.environ.get('HERMES_WEBUI_AUTO_INSTALL', '').strip().lower() in ('1', 'true', 'yes')
    if not enabled:
        print('[!!] Auto-install disabled. Set HERMES_WEBUI_AUTO_INSTALL=1 to enable.', flush=True)
        return False
    agent_dir = _agent_dir()
    if agent_dir is None:
        print('[!!] Auto-install skipped: agent directory not found.', flush=True)
        return False
    if not _trusted_agent_dir(agent_dir):
        print('[!!] Auto-install skipped: agent directory failed trust check (check ownership/permissions).', flush=True)
        return False
    req_file = agent_dir / 'requirements.txt'
    pyproject = agent_dir / 'pyproject.toml'
    if req_file.exists():
        install_args = [sys.executable, '-m', 'pip', 'install', '--quiet', '-r', str(req_file)]
        print(f'     Installing from {req_file} ...', flush=True)
    elif pyproject.exists():
        install_args = [sys.executable, '-m', 'pip', 'install', '--quiet', str(agent_dir)]
        print(f'     Installing from {agent_dir} (pyproject.toml) ...', flush=True)
    else:
        print('[!!] Auto-install skipped: no requirements.txt or pyproject.toml in agent dir.', flush=True)
        return False
    try:
        result = subprocess.run(install_args, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f'[!!] pip install failed (exit {result.returncode}):', flush=True)
            for line in (result.stderr or '').splitlines()[-10:]:
                print(f'     {line}', flush=True)
            return False
        print('[ok] pip install completed.', flush=True)
        return True
    except subprocess.TimeoutExpired:
        print('[!!] Auto-install timed out after 120s.', flush=True)
        return False
    except Exception as e:
        print(f'[!!] Auto-install error: {e}', flush=True)
        return False
