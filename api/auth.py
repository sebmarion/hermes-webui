"""
Hermes Web UI -- optional authentication.
Off by default. Enable by setting HERMES_WEBUI_PASSWORD, configuring a
password in Settings, registering passkeys, or configuring native OIDC SSO.
"""
import hashlib
import hmac
import http.cookies
import errno
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from api.config import STATE_DIR, get_config, load_settings

logger = logging.getLogger(__name__)

_MANAGED_KEY_OPEN_DIR_FD = os.open in os.supports_dir_fd
_MANAGED_KEY_STAT_DIR_FD = os.stat in os.supports_dir_fd
_MANAGED_KEY_STAT_NOFOLLOW = os.stat in os.supports_follow_symlinks
_MANAGED_KEY_LINK_DIR_FD = os.link in os.supports_dir_fd
_MANAGED_KEY_UNLINK_DIR_FD = os.unlink in os.supports_dir_fd
_MANAGED_KEY_QUARANTINE = ".signing_key.quarantine"
_MANAGED_KEY_QUARANTINE_BYTES = b"managed-signing-key-publication-v1\n"


# Default session TTL — 30 days. Kept as a module-level constant for backwards
# compatibility with downstream code and regression tests that import it.
# At runtime, prefer ``_resolve_session_ttl()`` which honours the env var and
# settings.json overrides; this constant is the floor / fallback.
SESSION_TTL = 86400 * 30  # 30 days


def _resolve_session_ttl() -> int:
    """Resolve session TTL from env > settings > default.

    Priority mirrors get_password_hash(): HERMES_WEBUI_SESSION_TTL env var
    first, then settings.json, falling back to ``SESSION_TTL`` (30 days).
    Clamped to [60s, 1 year] to prevent runaway cookies or self-lockout.
    """
    env_v = os.getenv('HERMES_WEBUI_SESSION_TTL', '').strip()
    if env_v.isdigit():
        val = int(env_v)
        if 60 <= val <= 86400 * 365:
            return val
    s = load_settings()
    v = s.get('session_ttl_seconds')
    if isinstance(v, int) and 60 <= v <= 86400 * 365:
        return v
    return SESSION_TTL


# ── Public paths (no auth required) ─────────────────────────────────────────
PUBLIC_PATHS = frozenset({
    '/login', '/health', '/favicon.ico', '/sw.js',
    '/api/auth/login', '/api/auth/status',
    '/api/auth/oidc/start', '/api/auth/oidc/callback',
    '/api/auth/passkey/options', '/api/auth/passkey/login',
    '/api/internal/recovery/start',
    '/api/internal/release-control',
    '/share',
    '/manifest.json', '/manifest.webmanifest',
    '/session/manifest.json', '/session/manifest.webmanifest',
})

COOKIE_NAME = 'hermes_session'
CSRF_HEADER_NAME = 'X-Hermes-CSRF-Token'


# RFC 6265 cookie-name token: a non-empty run of token chars
# (no controls, whitespace, or separators such as ';', '=', ',').
_COOKIE_NAME_RE = re.compile(r"^[-!#$%&'*+.^_`|~0-9A-Za-z]+$")


def _resolve_cookie_name() -> str:
    """Resolve the auth session cookie name from env > default.

    Honours ``HERMES_WEBUI_COOKIE_NAME`` so multiple WebUI instances sharing a
    hostname (different ports) can use distinct cookie names instead of
    trampling each other's session — browsers scope cookies by host, not
    host+port (RFC 6265). Falls back to ``COOKIE_NAME`` when the env var is
    unset, empty, or not a valid RFC 6265 token.
    """
    name = os.getenv('HERMES_WEBUI_COOKIE_NAME', '').strip()
    if not name:
        return COOKIE_NAME
    if _COOKIE_NAME_RE.match(name):
        return name
    logger.warning(
        'Ignoring invalid HERMES_WEBUI_COOKIE_NAME=%r; falling back to %r '
        '(name must be a valid RFC 6265 token)', name, COOKIE_NAME,
    )
    return COOKIE_NAME


def _warn_auth_persistence_failure(prefix: str, artifact: Path, exc: Exception, consequence: str) -> None:
    logger.warning(
        '%s at %s (STATE_DIR=%s): %s: %s; %s',
        prefix,
        artifact,
        STATE_DIR,
        exc.__class__.__name__,
        exc,
        consequence,
    )


_SESSIONS_FILE = STATE_DIR / '.sessions.json'
_TRUSTED_AUTH_HEADER_ENV = 'HERMES_WEBUI_TRUSTED_AUTH_HEADER'
_TRUSTED_GROUPS_HEADER_ENV = 'HERMES_WEBUI_TRUSTED_GROUPS_HEADER'
_TRUSTED_GROUP_PROFILE_MAP_ENV = 'HERMES_WEBUI_GROUP_PROFILE_MAP'
_TRUSTED_AUTH_LOGOUT_URL_ENV = 'HERMES_WEBUI_TRUSTED_AUTH_LOGOUT_URL'
_TRUSTED_AUTH_WARNINGS_EMITTED: set[str] = set()


def _warn_trusted_auth_once(key: str, message: str, *args) -> None:
    if key in _TRUSTED_AUTH_WARNINGS_EMITTED:
        return
    _TRUSTED_AUTH_WARNINGS_EMITTED.add(key)
    logger.warning(message, *args)


def _session_expiry(record) -> float | None:
    if isinstance(record, dict):
        expiry = record.get('expiry', record.get('expires_at'))
    else:
        expiry = record
    try:
        expiry_f = float(expiry)
    except (TypeError, ValueError):
        return None
    return expiry_f


def _load_sessions() -> dict[str, float | dict]:
    """Load persisted sessions from STATE_DIR, pruning expired entries.

    Returns an empty dict on any read or parse error so startup is never
    blocked by a corrupt or missing sessions file.
    """
    try:
        if not _SESSIONS_FILE.exists():
            return {}
        raw = _SESSIONS_FILE.read_text(encoding='utf-8')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('malformed sessions file: expected dict')
    except OSError as e:
        _warn_auth_persistence_failure(
            'Auth session store read failed',
            _SESSIONS_FILE,
            e,
            'starting fresh with an empty session table',
        )
        return {}
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        _warn_auth_persistence_failure(
            'Ignoring malformed auth session store',
            _SESSIONS_FILE,
            e,
            'starting fresh with an empty session table',
        )
        return {}
    except Exception as e:
        _warn_auth_persistence_failure(
            'Ignoring malformed auth session store',
            _SESSIONS_FILE,
            e,
            'starting fresh with an empty session table',
        )
        return {}
    now = time.time()
    sessions: dict[str, float | dict] = {}
    for token, record in data.items():
        if not isinstance(token, str) or not token:
            continue
        expiry = _session_expiry(record)
        if expiry is None or expiry <= now:
            continue
        if isinstance(record, dict):
            normalized = dict(record)
            normalized['expiry'] = expiry
            sessions[token] = normalized
        else:
            sessions[token] = expiry
    return sessions


def _save_sessions(sessions: dict[str, float | dict]) -> None:
    """Atomically persist sessions to STATE_DIR/.sessions.json (0600).

    Uses a temp file + os.replace() so a crash mid-write never leaves a
    truncated file.  Mirrors the same pattern as .signing_key persistence.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix='.sessions.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(sessions, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, _SESSIONS_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        _warn_auth_persistence_failure(
            'Auth session persistence failed',
            _SESSIONS_FILE,
            e,
            'keeping the in-process session table available',
        )


# Active sessions: token -> expiry timestamp (persisted across restarts via STATE_DIR)
_sessions = _load_sessions()
_SESSIONS_LOCK = threading.Lock()

# ── Login rate limiter ──────────────────────────────────────────────────────
_LOGIN_ATTEMPTS_FILE = STATE_DIR / '.login_attempts.json'
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 60  # seconds


def _load_login_attempts() -> dict[str, list[float]]:
    """Load persisted login attempts from STATE_DIR, pruning expired entries."""
    try:
        if _LOGIN_ATTEMPTS_FILE.exists():
            data = json.loads(_LOGIN_ATTEMPTS_FILE.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('malformed login-attempts file — expected dict')
            now = time.time()
            attempts: dict[str, list[float]] = {}
            for ip, raw_times in data.items():
                if not isinstance(ip, str) or not isinstance(raw_times, list):
                    continue
                fresh = [
                    float(t)
                    for t in raw_times
                    if isinstance(t, (int, float)) and now - float(t) < _LOGIN_WINDOW
                ]
                if fresh:
                    attempts[ip] = fresh
            return attempts
    except Exception as e:
        logger.debug("Failed to load login attempts file, starting fresh: %s", e)
    return {}


def _save_login_attempts(attempts: dict[str, list[float]]) -> None:
    """Atomically persist login attempts to STATE_DIR/.login_attempts.json (0600)."""
    try:
        _LOGIN_ATTEMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_LOGIN_ATTEMPTS_FILE.parent, suffix='.login_attempts.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(attempts, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, _LOGIN_ATTEMPTS_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("Failed to persist login attempts: %s", e)


_login_attempts = _load_login_attempts()  # ip -> [timestamp, ...]
_LOGIN_ATTEMPTS_LOCK = threading.Lock()


def _check_login_rate(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login (thread-safe)."""
    with _LOGIN_ATTEMPTS_LOCK:
        now = time.time()
        attempts = _login_attempts.get(ip, [])
        # Prune old attempts
        attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
        if attempts:
            _login_attempts[ip] = attempts
        else:
            _login_attempts.pop(ip, None)
        _save_login_attempts(_login_attempts)
        return len(attempts) < _LOGIN_MAX_ATTEMPTS


def _record_login_attempt(ip: str) -> None:
    """Record a login attempt for rate limiting (thread-safe)."""
    with _LOGIN_ATTEMPTS_LOCK:
        now = time.time()
        attempts = _login_attempts.get(ip, [])
        attempts.append(now)
        _login_attempts[ip] = attempts
        _save_login_attempts(_login_attempts)


def _clear_login_attempts(ip: str) -> None:
    """Clear failed login attempts after a successful login (thread-safe)."""
    with _LOGIN_ATTEMPTS_LOCK:
        if ip in _login_attempts:
            _login_attempts.pop(ip, None)
            _save_login_attempts(_login_attempts)


def _load_key(filename: str) -> bytes:
    """Load a 32-byte key from STATE_DIR, generating and persisting one if missing."""
    key_file = STATE_DIR / filename
    try:
        if key_file.exists():
            raw = key_file.read_bytes()
            if len(raw) >= 32:
                return raw[:32]
    except OSError as e:
        _warn_auth_persistence_failure(
            'Auth key read failed',
            key_file,
            e,
            'generating a new key and continuing',
        )
    except Exception as e:
        _warn_auth_persistence_failure(
            'Auth key read failed',
            key_file,
            e,
            'generating a new key and continuing',
        )
    key = secrets.token_bytes(32)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(key)
        key_file.chmod(0o600)
    except OSError as e:
        _warn_auth_persistence_failure(
            'Auth key persistence failed',
            key_file,
            e,
            'returning the generated key so startup can continue',
        )
    except Exception as e:
        _warn_auth_persistence_failure(
            'Auth key persistence failed',
            key_file,
            e,
            'returning the generated key so startup can continue',
        )
    return key


_PBKDF2_KEY_CACHE: bytes | None = None
_SIGNING_KEY_CACHE: bytes | None = None
_MANAGED_SIGNING_KEY_LOCK = threading.Lock()


class ManagedSigningKeyError(RuntimeError):
    """A managed signing-key postcondition could not be proven."""


class ManagedSigningKeyVerificationOutcome(str, Enum):
    PROVED_COMPLETE = "proved-complete"
    PROVED_ABSENT = "proved-absent"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ManagedSigningKeyPersistenceReceipt:
    key_path: str
    durable: bool
    created: bool


@dataclass(frozen=True)
class ManagedSigningKeyCacheReceipt:
    key_path: str
    cache_loaded: bool


@dataclass(frozen=True)
class ManagedSigningKeyVerification:
    outcome: ManagedSigningKeyVerificationOutcome
    reason: str | None
    persistence: ManagedSigningKeyPersistenceReceipt | None
    cache: ManagedSigningKeyCacheReceipt | None


@dataclass(frozen=True)
class _ManagedSigningKeyPaths:
    state_dir: str
    key_path: str


def _managed_signing_key_paths() -> _ManagedSigningKeyPaths:
    state_dir = os.path.abspath(os.fspath(STATE_DIR))
    return _ManagedSigningKeyPaths(
        state_dir=state_dir,
        key_path=os.path.join(state_dir, ".signing_key"),
    )


def _managed_key_same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _managed_key_validate_parent(value: os.stat_result) -> None:
    if not stat.S_ISDIR(value.st_mode):
        raise ManagedSigningKeyError("managed signing-key parent is not a directory")
    if hasattr(os, "getuid") and value.st_uid != os.getuid():
        raise ManagedSigningKeyError("managed signing-key parent has the wrong owner")
    if stat.S_IMODE(value.st_mode) & 0o077:
        raise ManagedSigningKeyError("managed signing-key parent is not owner-only")


def _managed_key_validate_file(value: os.stat_result) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise ManagedSigningKeyError("managed signing key is not a regular file")
    if hasattr(os, "getuid") and value.st_uid != os.getuid():
        raise ManagedSigningKeyError("managed signing key has the wrong owner")
    if value.st_nlink != 1:
        raise ManagedSigningKeyError("managed signing key has an unsafe link count")
    if stat.S_IMODE(value.st_mode) != 0o600:
        raise ManagedSigningKeyError("managed signing key mode is not 0600")
    if value.st_size != 32:
        raise ManagedSigningKeyError("managed signing key is not exactly 32 bytes")


def _managed_key_stat_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IMODE(value.st_mode),
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _managed_key_require_primitives() -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ManagedSigningKeyError("managed signing key requires O_NOFOLLOW")
    if not all(
        (
            _MANAGED_KEY_OPEN_DIR_FD,
            _MANAGED_KEY_STAT_DIR_FD,
            _MANAGED_KEY_STAT_NOFOLLOW,
            _MANAGED_KEY_LINK_DIR_FD,
            _MANAGED_KEY_UNLINK_DIR_FD,
        )
    ):
        raise ManagedSigningKeyError(
            "managed signing key requires no-follow dir_fd filesystem primitives"
        )


def _managed_key_open_parent(
    paths: _ManagedSigningKeyPaths,
) -> tuple[int, os.stat_result]:
    _managed_key_require_primitives()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent_fd: int | None = None
    try:
        path_stat = os.stat(paths.state_dir, follow_symlinks=False)
        parent_fd = os.open(paths.state_dir, flags)
        try:
            fd_stat = os.fstat(parent_fd)
        except BaseException:
            os.close(parent_fd)
            parent_fd = None
            raise
    except OSError as exc:
        raise ManagedSigningKeyError(
            "managed signing-key parent could not be opened safely"
        ) from exc
    try:
        _managed_key_validate_parent(path_stat)
        _managed_key_validate_parent(fd_stat)
        if not _managed_key_same_inode(path_stat, fd_stat):
            raise ManagedSigningKeyError(
                "managed signing-key parent identity changed while opening"
            )
        return parent_fd, fd_stat
    except BaseException:
        os.close(parent_fd)
        raise


def _managed_key_confirm_parent(
    paths: _ManagedSigningKeyPaths,
    parent_stat: os.stat_result,
) -> None:
    try:
        current = os.stat(paths.state_dir, follow_symlinks=False)
    except OSError as exc:
        raise ManagedSigningKeyError(
            "managed signing-key parent identity could not be rechecked"
        ) from exc
    _managed_key_validate_parent(current)
    if not _managed_key_same_inode(parent_stat, current):
        raise ManagedSigningKeyError(
            "managed signing-key parent identity changed during operation"
        )


def _managed_key_open_existing(parent_fd: int) -> int | None:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        return os.open(".signing_key", flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise ManagedSigningKeyError(
            "managed signing key could not be opened safely"
        ) from exc


def _managed_key_entry_stat(parent_fd: int) -> os.stat_result:
    try:
        return os.stat(".signing_key", dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise ManagedSigningKeyError(
            "managed signing key identity could not be verified"
        ) from exc


def _managed_key_read_pass(fd: int) -> bytes:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise ManagedSigningKeyError(
            "managed signing key could not be reread safely"
        ) from exc
    chunks: list[bytes] = []
    length = 0
    while length <= 32:
        try:
            chunk = os.read(fd, 33 - length)
        except OSError as exc:
            raise ManagedSigningKeyError(
                "managed signing key could not be read safely"
            ) from exc
        if not chunk:
            break
        chunks.append(chunk)
        length += len(chunk)
    return b"".join(chunks)


def _managed_key_read_fd_stable(fd: int) -> tuple[bytes, os.stat_result]:
    try:
        before = os.fstat(fd)
        first = _managed_key_read_pass(fd)
        middle = os.fstat(fd)
        second = _managed_key_read_pass(fd)
        after = os.fstat(fd)
    except OSError as exc:
        raise ManagedSigningKeyError(
            "managed signing key could not be inspected safely"
        ) from exc
    signature = _managed_key_stat_signature(before)
    if (
        signature != _managed_key_stat_signature(middle)
        or signature != _managed_key_stat_signature(after)
        or first != second
    ):
        raise ManagedSigningKeyError(
            "managed signing key contents or metadata changed during read"
        )
    if len(first) != 32:
        raise ManagedSigningKeyError(
            "managed signing key is not exactly 32 bytes"
        )
    return first, after


def _managed_key_read_existing(parent_fd: int) -> bytes | None:
    key_fd = _managed_key_open_existing(parent_fd)
    if key_fd is None:
        return None
    try:
        before = os.fstat(key_fd)
        entry_before = _managed_key_entry_stat(parent_fd)
        if not _managed_key_same_inode(before, entry_before):
            raise ManagedSigningKeyError(
                "managed signing key identity changed while opening"
            )
        _managed_key_validate_file(before)
        _managed_key_validate_file(entry_before)
        raw, after = _managed_key_read_fd_stable(key_fd)
        entry_after = _managed_key_entry_stat(parent_fd)
        if (
            not _managed_key_same_inode(before, after)
            or not _managed_key_same_inode(after, entry_after)
            or _managed_key_stat_signature(after)
            != _managed_key_stat_signature(entry_after)
        ):
            raise ManagedSigningKeyError(
                "managed signing key identity changed during read"
            )
        _managed_key_validate_file(after)
        _managed_key_validate_file(entry_after)
        return raw
    except OSError as exc:
        raise ManagedSigningKeyError(
            "managed signing key could not be read safely"
        ) from exc
    finally:
        os.close(key_fd)


def _managed_key_confirm_absent(parent_fd: int) -> None:
    try:
        os.stat(".signing_key", dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return
        raise ManagedSigningKeyError(
            "managed signing-key absence could not be rechecked"
        ) from exc
    raise ManagedSigningKeyError(
        "managed signing key appeared during absence verification"
    )


def _managed_key_assert_no_quarantine(parent_fd: int) -> None:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        quarantine_fd = os.open(_MANAGED_KEY_QUARANTINE, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return
        raise ManagedSigningKeyError(
            "managed signing-key quarantine state is unsafe"
        ) from exc
    try:
        value = os.fstat(quarantine_fd)
        if (
            not stat.S_ISREG(value.st_mode)
            or (hasattr(os, "getuid") and value.st_uid != os.getuid())
            or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise ManagedSigningKeyError(
                "managed signing-key quarantine state is unsafe"
            )
    finally:
        os.close(quarantine_fd)
    raise ManagedSigningKeyError(
        "managed signing-key durable quarantine requires explicit repair"
    )


def _managed_key_create_quarantine(parent_fd: int) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    quarantine_fd: int | None = None
    try:
        quarantine_fd = os.open(
            _MANAGED_KEY_QUARANTINE,
            flags,
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(quarantine_fd, 0o600)
        _managed_key_write_all(quarantine_fd, _MANAGED_KEY_QUARANTINE_BYTES)
        os.fsync(quarantine_fd)
        value = os.fstat(quarantine_fd)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_size != len(_MANAGED_KEY_QUARANTINE_BYTES)
        ):
            raise ManagedSigningKeyError(
                "managed signing-key quarantine could not be verified"
            )
        os.fsync(parent_fd)
    except FileExistsError as exc:
        raise ManagedSigningKeyError(
            "managed signing-key durable quarantine requires explicit repair"
        ) from exc
    except OSError as exc:
        raise ManagedSigningKeyError(
            "managed signing-key quarantine persistence failed"
        ) from exc
    finally:
        if quarantine_fd is not None:
            os.close(quarantine_fd)


def _managed_key_clear_quarantine(parent_fd: int) -> None:
    try:
        os.unlink(_MANAGED_KEY_QUARANTINE, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        raise ManagedSigningKeyError(
            "managed signing-key quarantine cleanup failed"
        ) from exc


def _managed_key_write_all(fd: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        try:
            written = os.write(fd, raw[offset:])
        except OSError as exc:
            raise ManagedSigningKeyError(
                "managed signing-key temporary file write failed"
            ) from exc
        if written <= 0:
            raise ManagedSigningKeyError(
                "managed signing-key temporary file write failed"
            )
        offset += written


def _managed_key_create(
    parent_fd: int,
    paths: _ManagedSigningKeyPaths,
) -> tuple[bytes, bool]:
    raw = secrets.token_bytes(32)
    temp_name = f".signing_key.tmp-{secrets.token_hex(16)}"
    temp_fd: int | None = None
    temp_exists = False
    published = False
    try:
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            temp_fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
            temp_exists = True
            os.fchmod(temp_fd, 0o600)
            _managed_key_write_all(temp_fd, raw)
            os.fsync(temp_fd)
            temp_stat = os.fstat(temp_fd)
        except OSError as exc:
            raise ManagedSigningKeyError(
                "managed signing-key temporary persistence failed"
            ) from exc
        _managed_key_validate_file(temp_stat)

        try:
            named_temp_stat = os.stat(
                temp_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ManagedSigningKeyError(
                "managed signing-key named temporary identity is unavailable"
            ) from exc
        if (
            not _managed_key_same_inode(temp_stat, named_temp_stat)
            or _managed_key_stat_signature(temp_stat)
            != _managed_key_stat_signature(named_temp_stat)
        ):
            raise ManagedSigningKeyError(
                "managed signing-key named temporary identity changed"
            )
        temp_raw, live_temp_stat = _managed_key_read_fd_stable(temp_fd)
        try:
            named_temp_stat = os.stat(
                temp_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ManagedSigningKeyError(
                "managed signing-key named temporary identity changed"
            ) from exc
        if (
            not hmac.compare_digest(temp_raw, raw)
            or _managed_key_stat_signature(live_temp_stat)
            != _managed_key_stat_signature(named_temp_stat)
        ):
            raise ManagedSigningKeyError(
                "managed signing-key named temporary contents changed"
            )

        _managed_key_create_quarantine(parent_fd)

        try:
            os.link(
                temp_name,
                ".signing_key",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            published = True
        except FileExistsError:
            published = False
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                published = False
            else:
                raise ManagedSigningKeyError(
                    "managed signing-key no-replace publish failed"
                ) from exc

        if published:
            published_stat = _managed_key_entry_stat(parent_fd)
            live_temp_stat = os.fstat(temp_fd)
            if (
                not _managed_key_same_inode(temp_stat, live_temp_stat)
                or not _managed_key_same_inode(live_temp_stat, published_stat)
            ):
                raise ManagedSigningKeyError(
                    "managed signing-key publication identity mismatch"
                )
            if (
                not stat.S_ISREG(live_temp_stat.st_mode)
                or live_temp_stat.st_uid != temp_stat.st_uid
                or stat.S_IMODE(live_temp_stat.st_mode) != 0o600
                or live_temp_stat.st_size != 32
                or live_temp_stat.st_nlink != 2
            ):
                raise ManagedSigningKeyError(
                    "managed signing-key publication metadata mismatch"
                )
            published_raw, reread_temp_stat = _managed_key_read_fd_stable(temp_fd)
            published_stat = _managed_key_entry_stat(parent_fd)
            try:
                named_temp_stat = os.stat(
                    temp_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise ManagedSigningKeyError(
                    "managed signing-key publication source disappeared"
                ) from exc
            if (
                not hmac.compare_digest(published_raw, raw)
                or not _managed_key_same_inode(reread_temp_stat, published_stat)
                or not _managed_key_same_inode(reread_temp_stat, named_temp_stat)
                or _managed_key_stat_signature(reread_temp_stat)
                != _managed_key_stat_signature(published_stat)
                or _managed_key_stat_signature(reread_temp_stat)
                != _managed_key_stat_signature(named_temp_stat)
            ):
                raise ManagedSigningKeyError(
                    "managed signing-key publication contents changed"
                )

        try:
            os.unlink(temp_name, dir_fd=parent_fd)
            temp_exists = False
        except OSError as exc:
            raise ManagedSigningKeyError(
                "managed signing-key temporary cleanup failed"
            ) from exc
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise ManagedSigningKeyError(
                "managed signing-key parent fsync failed"
            ) from exc

        if published:
            published_stat = _managed_key_entry_stat(parent_fd)
            published_raw, live_temp_stat = _managed_key_read_fd_stable(temp_fd)
            if not _managed_key_same_inode(live_temp_stat, published_stat):
                raise ManagedSigningKeyError(
                    "managed signing-key publication identity changed"
                )
            _managed_key_validate_file(live_temp_stat)
            _managed_key_validate_file(published_stat)
            if (
                not hmac.compare_digest(published_raw, raw)
                or _managed_key_stat_signature(live_temp_stat)
                != _managed_key_stat_signature(published_stat)
            ):
                raise ManagedSigningKeyError(
                    "managed signing-key publication contents are unverified"
                )

        winner = _managed_key_read_existing(parent_fd)
        if winner is None:
            raise ManagedSigningKeyError(
                "managed signing-key publication is missing"
            )
        if published and not hmac.compare_digest(winner, raw):
            raise ManagedSigningKeyError(
                "managed signing-key publication identity mismatch"
            )
        _managed_key_confirm_parent(paths, os.fstat(parent_fd))
        _managed_key_clear_quarantine(parent_fd)
        return winner, published
    except ManagedSigningKeyError:
        raise
    except OSError as exc:
        raise ManagedSigningKeyError(
            "managed signing-key persistence failed"
        ) from exc
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_exists:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass


def strict_persist_signing_key() -> ManagedSigningKeyPersistenceReceipt:
    """Durably persist or adopt the exact managed recovery signing key."""

    paths = _managed_signing_key_paths()
    with _MANAGED_SIGNING_KEY_LOCK:
        parent_fd, parent_stat = _managed_key_open_parent(paths)
        try:
            _managed_key_assert_no_quarantine(parent_fd)
            raw = _managed_key_read_existing(parent_fd)
            created = False
            if raw is None:
                raw, created = _managed_key_create(parent_fd, paths)
            _managed_key_confirm_parent(paths, parent_stat)
            confirmed = _managed_key_read_existing(parent_fd)
            _managed_key_confirm_parent(paths, parent_stat)
            if (
                confirmed is None
                or len(raw) != 32
                or not hmac.compare_digest(raw, confirmed)
            ):
                raise ManagedSigningKeyError(
                    "managed signing-key durable postcondition failed"
                )
            _managed_key_assert_no_quarantine(parent_fd)
            return ManagedSigningKeyPersistenceReceipt(
                key_path=paths.key_path,
                durable=True,
                created=created,
            )
        finally:
            os.close(parent_fd)


def strict_load_signing_key_cache() -> ManagedSigningKeyCacheReceipt:
    """Load the verified durable key into the current-process cache."""

    global _SIGNING_KEY_CACHE
    paths = _managed_signing_key_paths()
    with _MANAGED_SIGNING_KEY_LOCK:
        parent_fd, parent_stat = _managed_key_open_parent(paths)
        try:
            _managed_key_assert_no_quarantine(parent_fd)
            raw = _managed_key_read_existing(parent_fd)
            if raw is None:
                raise ManagedSigningKeyError(
                    "managed signing-key durable file is absent"
                )
            current = _SIGNING_KEY_CACHE
            if current is not None and (
                not isinstance(current, bytes)
                or len(current) != 32
                or not hmac.compare_digest(current, raw)
            ):
                raise ManagedSigningKeyError(
                    "managed signing-key cache does not match durable file"
                )
            _managed_key_confirm_parent(paths, parent_stat)
            confirmed = _managed_key_read_existing(parent_fd)
            _managed_key_confirm_parent(paths, parent_stat)
            if confirmed is None or not hmac.compare_digest(raw, confirmed):
                raise ManagedSigningKeyError(
                    "managed signing-key durable file changed before cache load"
                )
            _managed_key_assert_no_quarantine(parent_fd)
            _SIGNING_KEY_CACHE = confirmed
            if not hmac.compare_digest(_SIGNING_KEY_CACHE, confirmed):
                raise ManagedSigningKeyError(
                    "managed signing-key cache postcondition failed"
                )
            return ManagedSigningKeyCacheReceipt(
                key_path=paths.key_path,
                cache_loaded=True,
            )
        finally:
            os.close(parent_fd)


def verify_strict_signing_key() -> ManagedSigningKeyVerification:
    """Mutation-free verification of durable and process-cache postconditions."""

    paths = _managed_signing_key_paths()
    with _MANAGED_SIGNING_KEY_LOCK:
        try:
            parent_fd, parent_stat = _managed_key_open_parent(paths)
            try:
                _managed_key_assert_no_quarantine(parent_fd)
                raw = _managed_key_read_existing(parent_fd)
                if raw is None:
                    _managed_key_confirm_absent(parent_fd)
                _managed_key_confirm_parent(paths, parent_stat)
                _managed_key_assert_no_quarantine(parent_fd)
                confirmed = _managed_key_read_existing(parent_fd)
                if raw is None:
                    if confirmed is not None:
                        raise ManagedSigningKeyError(
                            "managed signing key appeared during absence verification"
                        )
                    _managed_key_confirm_absent(parent_fd)
                elif confirmed is None or not hmac.compare_digest(raw, confirmed):
                    raise ManagedSigningKeyError(
                        "managed signing key changed during verification"
                    )
                _managed_key_confirm_parent(paths, parent_stat)
            finally:
                os.close(parent_fd)
        except ManagedSigningKeyError:
            return ManagedSigningKeyVerification(
                ManagedSigningKeyVerificationOutcome.AMBIGUOUS,
                "unsafe_durable_file",
                None,
                None,
            )

        current = _SIGNING_KEY_CACHE
        if raw is None:
            if current is None:
                return ManagedSigningKeyVerification(
                    ManagedSigningKeyVerificationOutcome.PROVED_ABSENT,
                    "durable_file_absent",
                    None,
                    None,
                )
            return ManagedSigningKeyVerification(
                ManagedSigningKeyVerificationOutcome.AMBIGUOUS,
                "cache_without_durable_file",
                None,
                None,
            )

        persistence = ManagedSigningKeyPersistenceReceipt(
            key_path=paths.key_path,
            durable=True,
            created=False,
        )
        if current is None:
            return ManagedSigningKeyVerification(
                ManagedSigningKeyVerificationOutcome.PARTIAL,
                "durable_file_cache_absent",
                persistence,
                None,
            )
        if (
            not isinstance(current, bytes)
            or len(current) != 32
            or not hmac.compare_digest(current, raw)
        ):
            return ManagedSigningKeyVerification(
                ManagedSigningKeyVerificationOutcome.AMBIGUOUS,
                "cache_mismatch",
                persistence,
                None,
            )
        cache = ManagedSigningKeyCacheReceipt(
            key_path=paths.key_path,
            cache_loaded=True,
        )
        return ManagedSigningKeyVerification(
            ManagedSigningKeyVerificationOutcome.PROVED_COMPLETE,
            None,
            persistence,
            cache,
        )


def _pbkdf2_key() -> bytes:
    global _PBKDF2_KEY_CACHE
    if _PBKDF2_KEY_CACHE is None:
        _PBKDF2_KEY_CACHE = _load_key('.pbkdf2_key')
    return _PBKDF2_KEY_CACHE


def _signing_key() -> bytes:
    global _SIGNING_KEY_CACHE
    if _SIGNING_KEY_CACHE is None:
        _SIGNING_KEY_CACHE = _load_key('.signing_key')
    return _SIGNING_KEY_CACHE


def _hash_password(password, *, salt: bytes | None = None) -> str:
    """PBKDF2-SHA256 with 600k iterations (OWASP recommendation).
    Salt is the persisted PBKDF2 key, which is secret and unique per
    installation. This keeps the stored hash format a plain hex string
    (no format change to settings.json) while replacing the predictable
    STATE_DIR-derived salt from the original implementation.

    The *salt* parameter exists solely to support transparent migration
    of password hashes that were computed with a different key (e.g. the
    old `.signing_key`). Normal callers should never pass it.
    """
    if salt is None:
        salt = _pbkdf2_key()
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)
    return dk.hex()


_AUTH_HASH_LOCK = threading.Lock()
_AUTH_HASH_COMPUTED: bool = False
_AUTH_HASH_CACHE: str | None = None


def _invalidate_password_hash_cache() -> None:
    """Invalidate the in-process password hash cache so the next call to
    get_password_hash() re-reads from settings.json or the env var."""
    global _AUTH_HASH_COMPUTED, _AUTH_HASH_CACHE
    with _AUTH_HASH_LOCK:
        _AUTH_HASH_COMPUTED = False
        _AUTH_HASH_CACHE = None


def get_password_hash() -> str | None:
    """Return the active password hash, or None if auth is disabled.
    Priority: env var > settings.json.

    The hash is computed once and cached for the lifetime of the process.
    PBKDF2-600k takes ~1 s and is called on nearly every HTTP request via
    check_auth → is_auth_enabled, so caching avoids wasting a full second
    of CPU per request after the first one.

    Thread-safe: double-checked locking ensures that under a burst of
    concurrent requests only one thread computes PBKDF2, while the fast
    path (after initialisation) requires zero locks.
    """
    global _AUTH_HASH_COMPUTED, _AUTH_HASH_CACHE

    # Fast path — no lock needed once cache is populated.
    if _AUTH_HASH_COMPUTED:
        return _AUTH_HASH_CACHE

    with _AUTH_HASH_LOCK:
        # Re-check inside lock — another thread may have populated while
        # we were waiting to acquire.
        if _AUTH_HASH_COMPUTED:
            return _AUTH_HASH_CACHE

        env_pw = os.getenv('HERMES_WEBUI_PASSWORD', '').strip()
        if env_pw:
            result = _hash_password(env_pw)
        else:
            result = load_settings().get('password_hash') or None

        _AUTH_HASH_CACHE = result
        _AUTH_HASH_COMPUTED = True
        return result


def is_password_auth_enabled() -> bool:
    """True if a password is configured (env var or settings)."""
    return get_password_hash() is not None


def _passkey_feature_flag_enabled() -> bool:
    """Return True if the passkey/WebAuthn surface is enabled for this deployment.

    Passkey support is opt-in default-off behind a feature flag so deployments
    that don't want the WebAuthn surface (or whose RP-ID setup isn't ready for
    non-localhost hosts) can disable it entirely with no UI surface, no
    endpoints, no credential storage. To enable:

      - Set ``HERMES_WEBUI_PASSKEY=1`` in the environment, OR
      - Set ``webui_passkey_enabled: true`` in the per-profile config.yaml

    With the flag off, ``are_passkeys_enabled()`` always returns False even if
    credentials were registered in the past, and ``/login`` shows password-only.
    """
    env_value = os.getenv("HERMES_WEBUI_PASSKEY", "")
    if env_value:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from api.config import get_config

        cfg = get_config()
        if isinstance(cfg, dict):
            raw = cfg.get("webui_passkey_enabled")
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        pass
    return False


def are_passkeys_enabled() -> bool:
    """True if the passkey feature flag is on AND at least one local passkey credential is registered."""
    if not _passkey_feature_flag_enabled():
        return False
    try:
        from api.passkeys import passkeys_available

        return passkeys_available()
    except Exception as exc:
        logger.debug("Failed to inspect passkey availability: %s", exc)
        return False


def is_oidc_auth_enabled() -> bool:
    """True if native OIDC login is configured for WebUI sessions."""
    try:
        from api.auth_oidc import is_oidc_enabled

        return is_oidc_enabled()
    except Exception as exc:
        logger.debug("Failed to inspect OIDC availability: %s", exc)
        return False


def get_oidc_startup_warning() -> str | None:
    """Return a startup warning when OIDC auth is only partially configured,
    or when allow_values uses whitespace that is no longer a separator."""
    try:
        cfg = get_config()
        raw = cfg.get("webui_oidc") if isinstance(cfg, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        logger.debug("Failed to read webui_oidc config", exc_info=True)
        raw = {}

    def pick(name: str, env_name: str) -> str:
        env_value = os.getenv(env_name)
        value = env_value if env_value is not None else raw.get(name)
        return str(value or "").strip()

    issuer = bool(pick("issuer", "HERMES_WEBUI_OIDC_ISSUER"))
    client_id = bool(pick("client_id", "HERMES_WEBUI_OIDC_CLIENT_ID"))
    allow_claim = bool(pick("allow_claim", "HERMES_WEBUI_OIDC_ALLOW_CLAIM"))
    raw_allow_env = os.getenv("HERMES_WEBUI_OIDC_ALLOW_VALUES")
    raw_allow = raw_allow_env if raw_allow_env is not None else raw.get("allow_values")
    normalized_allow_values = []
    allow_values_warning = None
    try:
        from api import auth_oidc

        normalized_allow_values = auth_oidc._normalize_allow_values(raw_allow)
        allow_values_warning = auth_oidc._ALLOW_VALUES_WHITESPACE_WARNING
    except Exception:
        logger.debug("Failed to normalize OIDC allow_values", exc_info=True)
    allow_values = bool(normalized_allow_values)

    if not any((issuer, client_id, allow_claim, allow_values)):
        return None

    warnings = []

    if not (issuer and client_id and allow_claim and allow_values):
        missing = []
        if not issuer:
            missing.append("issuer")
        if not client_id:
            missing.append("client_id")
        if not allow_claim:
            missing.append("allow_claim")
        if not allow_values:
            missing.append("allow_values")
        joined = ", ".join(missing)
        warnings.append(
            "Native OIDC login is only partially configured; missing "
            f"{joined}. The WebUI will not enable OIDC auth until all four fields are set."
        )

    # Detect whitespace-only allow_values scalar that may contain multiple intended values.
    # Runs unconditionally so the warning reaches startup even when other auth methods
    # short-circuit is_auth_enabled() before the OIDC branch is evaluated.
    if (
        allow_values_warning is not None
        and raw_allow is not None
        and not isinstance(raw_allow, (list, tuple, set))
        and any(any(ch.isspace() for ch in v) for v in normalized_allow_values)
    ):
        warnings.append(allow_values_warning)

    return "\n".join(warnings) if warnings else None


def is_auth_enabled() -> bool:
    """True if password auth, passkeys, OIDC login, or trusted-header auth is configured."""
    return (
        is_password_auth_enabled()
        or are_passkeys_enabled()
        or is_oidc_auth_enabled()
        or is_trusted_auth_enabled()
    )


def verify_password(plain: str) -> bool:
    """Verify a plaintext password against the stored hash.

    Supports transparent migration of password hashes that were computed
    with the old `.signing_key` salt.  When the two keys differ and the
    legacy-salted hash matches, the password is transparently re-hashed
    with the current `.pbkdf2_key` and persisted to settings.json.
    """
    expected = get_password_hash()
    if not expected:
        return False
    # Fast path: current PBKDF2 key
    if hmac.compare_digest(_hash_password(plain), expected):
        return True
    # Migration: some hashes were computed with `.signing_key` before the
    # PBKDF2 key was separated.  Try the legacy salt; if it matches,
    # transparently upgrade so the next login uses the fast path.
    legacy_salt = _signing_key()
    current_salt = _pbkdf2_key()
    if legacy_salt != current_salt:
        if hmac.compare_digest(_hash_password(plain, salt=legacy_salt), expected):
            from api.config import save_settings

            save_settings({'_set_password': plain})
            # Password re-hashed and persisted to disk using the current salt.
            # Cache invalidation is handled by fix 2/3 (#2192) which adds the
            # _invalidate_password_hash_cache() call inside save_settings().
            return True
    return False


def create_session(*, auth_type: str | None = None, username: str | None = None, bound_profile: str | None = None) -> str:
    """Create a new auth session. Returns signed cookie value."""
    token = secrets.token_hex(32)
    expiry = time.time() + _resolve_session_ttl()
    record: float | dict
    if any(value is not None for value in (auth_type, username, bound_profile)):
        record = {
            'expiry': expiry,
            'auth_type': auth_type,
            'username': username,
            'bound_profile': bound_profile,
        }
    else:
        record = expiry
    with _SESSIONS_LOCK:
        _sessions[token] = record
        _save_sessions(_sessions)
    sig = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()
    return f"{token}.{sig}"


def _prune_expired_sessions():
    """Remove all expired session entries to prevent unbounded memory growth."""
    now = time.time()
    with _SESSIONS_LOCK:
        expired = [t for t, record in _sessions.items() if (expiry := _session_expiry(record)) is None or now > expiry]
        if expired:
            for token in expired:
                _sessions.pop(token, None)
            _save_sessions(_sessions)


def verify_session(cookie_value: str) -> bool:
    """Verify a signed session cookie. Returns True if valid and not expired."""
    if not cookie_value or '.' not in cookie_value:
        return False
    _prune_expired_sessions()  # lazy cleanup on every verification attempt
    token, sig = cookie_value.rsplit('.', 1)
    full_sig = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()
    # Accept both new (64-char) and legacy (32-char truncated) signatures so
    # existing sessions survive the upgrade without a forced global logout.
    # The legacy branch can be removed once session TTLs have expired (~30 days).
    valid = hmac.compare_digest(sig, full_sig) or (
        len(sig) == 32 and hmac.compare_digest(sig, full_sig[:32])
    )
    if not valid:
        return False
    with _SESSIONS_LOCK:
        expiry = _session_expiry(_sessions.get(token))
        if expiry is None or time.time() > expiry:
            _sessions.pop(token, None)
            _save_sessions(_sessions)
            return False
    return True


def _trusted_auth_header_name() -> str | None:
    name = os.getenv(_TRUSTED_AUTH_HEADER_ENV, '').strip()
    if not name:
        return None
    if not _COOKIE_NAME_RE.match(name):
        _warn_trusted_auth_once(
            'trusted-auth-header',
            'Ignoring invalid %s=%r; trusted-header auth rejects every request',
            _TRUSTED_AUTH_HEADER_ENV,
            name,
        )
        return None
    return name


def _trusted_auth_header_configured() -> bool:
    return bool(os.getenv(_TRUSTED_AUTH_HEADER_ENV, '').strip())


def _trusted_group_profile_map() -> dict[str, str] | None:
    raw = os.getenv(_TRUSTED_GROUP_PROFILE_MAP_ENV, '').strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _warn_trusted_auth_once(
            'trusted-group-map',
            'Ignoring invalid %s JSON; trusted-header auth falls back to default profile binding',
            _TRUSTED_GROUP_PROFILE_MAP_ENV,
        )
        return {}
    if not isinstance(data, dict):
        _warn_trusted_auth_once(
            'trusted-group-map-type',
            'Ignoring non-dict %s; trusted-header auth falls back to default profile binding',
            _TRUSTED_GROUP_PROFILE_MAP_ENV,
        )
        return {}
    mapping: dict[str, str] = {}
    for group, profile in data.items():
        group_name = str(group or '').strip()
        profile_name = str(profile or '').strip()
        if not group_name or not profile_name:
            _warn_trusted_auth_once(
                'trusted-group-map-entry',
                'Ignoring invalid entry in %s; trusted-header auth falls back to default profile binding',
                _TRUSTED_GROUP_PROFILE_MAP_ENV,
            )
            continue
        mapping[group_name] = profile_name
    return mapping


def _trusted_groups_header_value(handler) -> list[str]:
    header_name = os.getenv(_TRUSTED_GROUPS_HEADER_ENV, '').strip()
    if not header_name:
        return []
    try:
        raw = handler.headers.get(header_name, '')
    except Exception:
        return []
    if not raw:
        return []
    values = []
    for part in str(raw).replace('\n', ',').split(','):
        part = part.strip()
        if part:
            values.append(part)
    return values


def _trusted_auth_username(handler) -> str | None:
    header_name = _trusted_auth_header_name()
    if not header_name:
        return None
    try:
        raw = handler.headers.get(header_name, '')
    except Exception:
        return None
    username = str(raw or '').strip()
    return username or None


def _trusted_auth_bound_profile(handler) -> str | None:
    mapping = _trusted_group_profile_map()
    if mapping is None:
        return None
    groups = set(_trusted_groups_header_value(handler))
    for group, profile in mapping.items():
        if group in groups:
            return profile
    return 'default'


def _queue_pending_cookie(handler, cookie_header: str) -> None:
    if not cookie_header:
        return
    pending = getattr(handler, '_pending_set_cookies', None)
    if pending is None:
        pending = []
        handler._pending_set_cookies = pending
    pending.append(cookie_header)


def _auth_cookie_header(cookie_value, handler=None) -> str:
    cookie = http.cookies.SimpleCookie()
    name = _resolve_cookie_name()
    cookie[name] = cookie_value
    cookie[name]['httponly'] = True
    cookie[name]['samesite'] = 'Lax'
    cookie[name]['path'] = '/'
    cookie[name]['max-age'] = str(_resolve_session_ttl())
    if _is_secure_context(handler):
        cookie[name]['secure'] = True
    return cookie[name].OutputString()


def _clear_auth_cookie_header() -> str:
    cookie = http.cookies.SimpleCookie()
    name = _resolve_cookie_name()
    cookie[name] = ''
    cookie[name]['httponly'] = True
    cookie[name]['path'] = '/'
    cookie[name]['samesite'] = 'Lax'
    cookie[name]['max-age'] = '0'
    return cookie[name].OutputString()


def _build_profile_cookie_header(name: str, session_cookie_value: str | None) -> str:
    from api.helpers import build_profile_cookie

    return build_profile_cookie(name, session_cookie_value=session_cookie_value)


def _request_profile_matches_bound(bound_profile: str | None) -> bool:
    if not bound_profile:
        return True
    try:
        from api.profiles import get_active_profile_name, _profiles_match

        return _profiles_match(bound_profile, get_active_profile_name())
    except Exception:
        return False


def get_session_info(cookie_value: str) -> dict | None:
    if not verify_session(cookie_value):
        return None
    token = _session_token_from_cookie_value(cookie_value)
    if not token:
        return None
    with _SESSIONS_LOCK:
        record = _sessions.get(token)
    expiry = _session_expiry(record)
    if expiry is None:
        return None
    info: dict[str, object] = {'token': token, 'expiry': expiry}
    if isinstance(record, dict):
        info.update({k: v for k, v in record.items() if k != 'expiry'})
    if 'bound_profile' not in info and isinstance(info.get('profile'), str):
        info['bound_profile'] = info.get('profile')
    info.setdefault('auth_type', None)
    info.setdefault('username', None)
    info.setdefault('bound_profile', None)
    return info


def session_bound_profile(cookie_value: str) -> str | None:
    info = get_session_info(cookie_value)
    if not info:
        return None
    bound_profile = info.get('bound_profile')
    bound_profile = str(bound_profile or '').strip()
    return bound_profile or None


def is_trusted_auth_enabled() -> bool:
    return _trusted_auth_header_configured()


def get_trusted_auth_logout_url() -> str | None:
    value = os.getenv(_TRUSTED_AUTH_LOGOUT_URL_ENV, '').strip()
    return value or None


def _remember_trusted_auth_session(handler, info: dict | None, cookie_value: str | None = None) -> dict | None:
    handler._trusted_auth_session_reconciled = info
    if info and info.get('auth_type') == 'trusted':
        handler._trusted_auth_session_info = info
        handler._trusted_auth_session_cookie_value = cookie_value
    return info


def reset_trusted_auth_request_state(handler) -> None:
    for name in (
        '_trusted_auth_session_reconciled',
        '_trusted_auth_session_rejected',
        '_trusted_auth_session_info',
        '_trusted_auth_session_cookie_value',
        # Clear any auth cookie queued by a prior request but not yet flushed.
        # The handler is reused across HTTP/1.1 keep-alive requests, so a stale
        # queued Set-Cookie would otherwise cross the request boundary and be
        # emitted by a later response — e.g. after trusted-identity rotation on
        # logout it could overwrite a subsequent valid login cookie and 401 the
        # user. Reset it at the per-request boundary (server.py do_GET/do_POST).
        '_pending_set_cookies',
    ):
        try:
            delattr(handler, name)
        except AttributeError:
            pass


def _apply_trusted_session_profile(handler, bound_profile: str | None, cookie_value: str) -> None:
    if bound_profile is None:
        return
    from api.helpers import get_profile_cookie
    from api.profiles import set_request_profile

    set_request_profile(bound_profile)
    if get_profile_cookie(handler) != bound_profile:
        _queue_pending_cookie(handler, _build_profile_cookie_header(bound_profile, cookie_value))


def ensure_trusted_auth_session(handler) -> dict | None:
    if hasattr(handler, '_trusted_auth_session_reconciled'):
        return handler._trusted_auth_session_reconciled
    cookie_value = parse_cookie(handler)
    info = get_session_info(cookie_value) if cookie_value and verify_session(cookie_value) else None
    if info and info.get('auth_type') != 'trusted':
        return _remember_trusted_auth_session(handler, info)
    if not is_trusted_auth_enabled():
        if info:
            invalidate_session(cookie_value)
            handler._trusted_auth_session_rejected = True
        return _remember_trusted_auth_session(handler, None)
    from api.routes import _raw_peer_is_trusted_proxy

    if not _raw_peer_is_trusted_proxy(handler):
        if info:
            invalidate_session(cookie_value)
            handler._trusted_auth_session_rejected = True
        return _remember_trusted_auth_session(handler, None)
    username = _trusted_auth_username(handler)
    if not username:
        if info:
            invalidate_session(cookie_value)
            handler._trusted_auth_session_rejected = True
        return _remember_trusted_auth_session(handler, None)
    bound_profile = _trusted_auth_bound_profile(handler)
    if info and info.get('username') == username and info.get('bound_profile') == bound_profile:
        _apply_trusted_session_profile(handler, bound_profile, cookie_value)
        return _remember_trusted_auth_session(handler, info, cookie_value)
    if info:
        invalidate_session(cookie_value)
    cookie_value = create_session(
        auth_type='trusted',
        username=username,
        bound_profile=bound_profile,
    )
    _queue_pending_cookie(handler, _auth_cookie_header(cookie_value, handler))
    _apply_trusted_session_profile(handler, bound_profile, cookie_value)
    info = get_session_info(cookie_value)
    return _remember_trusted_auth_session(handler, info, cookie_value)


def trusted_session_allows_active_profile(info: dict | None) -> bool:
    if not info:
        return True
    return _request_profile_matches_bound(str(info.get('bound_profile') or '') or None)


def _session_token_from_cookie_value(cookie_value: str) -> str | None:
    """Return the raw server-side session token from a signed cookie value."""
    if not cookie_value or '.' not in cookie_value:
        return None
    token, _sig = cookie_value.rsplit('.', 1)
    return token or None


def sign_profile_cookie_value(profile_name: str, session_cookie_value: str | None) -> str:
    """Return a profile cookie value authenticated for one WebUI session.

    The active-profile cookie is client-controlled, so when auth is enabled it
    must not be trusted as a bare profile name. Binding the selected profile to
    the HttpOnly session token prevents a client from forging
    ``hermes_profile=<other-profile>`` and bypassing profile visibility guards.
    """
    if not session_cookie_value or not verify_session(session_cookie_value):
        raise ValueError("active auth session is required to sign profile cookie")
    token = _session_token_from_cookie_value(session_cookie_value)
    if not token:
        raise ValueError("active auth session is required to sign profile cookie")
    sig = hmac.new(
        _signing_key(),
        f"profile:{token}:{profile_name}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{profile_name}.{sig}"


def verify_profile_cookie_value(cookie_value: str, session_cookie_value: str | None) -> str | None:
    """Verify a session-bound profile cookie and return its profile name."""
    if not cookie_value or '.' not in cookie_value:
        return None
    if not session_cookie_value or not verify_session(session_cookie_value):
        return None
    profile_name, sig = cookie_value.rsplit('.', 1)
    token = _session_token_from_cookie_value(session_cookie_value)
    if not profile_name or not token or not sig:
        return None
    # Defense-in-depth: validate the profile-name pattern here too, not only in
    # get_profile_cookie(), so any future caller of this verifier can't return an
    # unvalidated name. (#4023 Opus hardening.)
    from api.profiles import _PROFILE_ID_RE
    if profile_name != 'default' and not _PROFILE_ID_RE.fullmatch(profile_name):
        return None
    expected = hmac.new(
        _signing_key(),
        f"profile:{token}:{profile_name}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(str(sig), expected):
        return profile_name
    return None


def csrf_token_for_session(cookie_value: str) -> str | None:
    """Return the CSRF token bound to an authenticated WebUI session.

    The browser can read this token from the authenticated shell and echoes it
    in ``X-Hermes-CSRF-Token`` on unsafe API requests. The token is derived
    from the HttpOnly session cookie's server-side token, so it automatically
    rotates on login and is invalidated when the auth session expires or logs
    out. Callers must still verify the auth session before trusting it.
    """
    token = _session_token_from_cookie_value(cookie_value)
    if not token:
        return None
    return hmac.new(_signing_key(), f"csrf:{token}".encode(), hashlib.sha256).hexdigest()


def verify_csrf_token(cookie_value: str, csrf_token: str) -> bool:
    """Verify a submitted CSRF token against the authenticated session."""
    if not cookie_value or not csrf_token or not verify_session(cookie_value):
        return False
    expected = csrf_token_for_session(cookie_value)
    return bool(expected and hmac.compare_digest(str(csrf_token), expected))


def invalidate_session(cookie_value) -> None:
    """Remove a session token."""
    if cookie_value and '.' in cookie_value:
        token = cookie_value.rsplit('.', 1)[0]
        with _SESSIONS_LOCK:
            if token in _sessions:
                _sessions.pop(token, None)
                _save_sessions(_sessions)


def parse_cookie(handler) -> str | None:
    """Extract the auth cookie from the request headers."""
    cookie_header = handler.headers.get('Cookie', '')
    if not cookie_header:
        return None
    cookie = http.cookies.SimpleCookie()
    try:
        cookie.load(cookie_header)
    except http.cookies.CookieError:
        return None
    morsel = cookie.get(_resolve_cookie_name())
    return morsel.value if morsel else None


def _safe_login_inner_next(query: str | None) -> str:
    """#5578: extract a SAFE, non-login inner redirect from a login page's query.

    When an expired-auth bounce lands back on the login page (which already
    carries its own `next` in the query), we want to preserve a legitimate inner
    destination X across the redirect to the real login route — but only if X is
    itself safe (path-absolute, not protocol-relative/backslash, no control
    chars) AND not login-shaped / not itself carrying a nested next param.
    Anything else collapses to '' (no inner redirect), which kills the
    self-referential chain. Mirrors _safe_login_redirect_path().
    """
    import urllib.parse as _u
    raw = _u.parse_qs(query or "").get("next", [""])[0]
    path = str(raw or "").strip()
    if not path or path[0] != "/" or path[1:2] in {"/", "\\"}:
        return ""
    if re.search(r"[\x00-\x1f\x7f\s]", path) or len(path) > 2048:
        return ""
    # Collapse only login-route chains — decode a few levels so a nested
    # `/session/login%3Fnext%3D...` (encoded `?`) is still recognized by its
    # leading PATH — but preserve a legitimate non-login inner path that merely
    # carries its own `next=` query key (e.g. `/admin?next=/real/path`).
    _probe = path
    for _ in range(8):
        _p = _probe.split("?", 1)[0].split("#", 1)[0].split("&", 1)[0].rstrip("/")
        if _p == "/login" or _p.endswith("/login"):
            return ""
        _decoded = _u.unquote(_probe)
        if _decoded == _probe:
            break
        _probe = _decoded
    else:
        # Still decoding at the cap (pathologically deep encoding) → fail closed.
        _p = _probe.split("?", 1)[0].split("#", 1)[0].split("&", 1)[0].rstrip("/")
        if _p == "/login" or _p.endswith("/login"):
            return ""
        return ""
    return path


def check_auth(handler, parsed) -> bool:
    """Check if request is authorized. Returns True if OK.
    If not authorized, sends 401 (API) or 302 redirect (page) and returns False."""
    if not is_auth_enabled():
        return True
    # Public paths don't require auth
    if (
        parsed.path in PUBLIC_PATHS
        or parsed.path.startswith('/share/')
        or (
            parsed.path.startswith('/api/share/')
            and parsed.path not in {'/api/share/create', '/api/share/revoke'}
        )
        or parsed.path.startswith('/static/')
        or parsed.path.startswith('/session/static/')
    ):
        return True
    cookie_val = parse_cookie(handler)
    has_session = bool(cookie_val and verify_session(cookie_val))
    if parsed.path == '/api/auth/logout':
        if has_session:
            return True
        body = b'{"error":"Authentication required"}'
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return False
    session_info = ensure_trusted_auth_session(handler)
    if session_info:
        if not trusted_session_allows_active_profile(session_info):
            if parsed.path.startswith('/api/'):
                body = b'{"error":"Profile access forbidden"}'
                handler.send_response(403)
                handler.send_header('Content-Type', 'application/json')
            else:
                body = b'Profile access forbidden'
                handler.send_response(403)
                handler.send_header('Content-Type', 'text/plain; charset=utf-8')
            handler.send_header('Content-Length', str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return False
        return True
    # Not authorized
    if parsed.path.startswith('/api/'):
        body = b'{"error":"Authentication required"}'
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    else:
        handler.send_response(302)
        # Pass the original path as ?next= so login.js redirects back after auth.
        # SECURITY/CORRECTNESS: the inner `?` and `&` MUST be percent-encoded
        # when stuffed into the outer `?next=` parameter, otherwise:
        #   (a) multi-param query strings get truncated at the first inner `&`
        #       (e.g. `/api/sessions?limit=50&offset=0` would round-trip as
        #       just `/api/sessions?limit=50` after the browser parses the
        #       outer URL — `offset=0` becomes a separate top-level query
        #       parameter that the login page ignores).
        #   (b) attacker-controlled paths could inject a second `next=`
        #       parameter; per RFC 3986 the duplicate behaviour is undefined
        #       and parsers diverge (Python's parse_qs returns last-match,
        #       URLSearchParams returns first-match), opening a query-pollution
        #       footgun even though _safeNextPath() rejects most malicious
        #       shapes downstream.
        # Encoding the entire `path?query` blob with quote(safe='/') turns
        # `?` → `%3F` and `&` → `%26`, so the outer parameter holds exactly
        # one path-with-query string and `searchParams.get('next')` returns
        # the full original URL (the browser auto-decodes once).
        # (Opus pre-release advisor finding for v0.50.258.)
        import urllib.parse as _urlparse
        # #5578: if the page being redirected is ALREADY login-shaped, do NOT
        # wrap its full `path?query` into a fresh `next=` — that query already
        # carries a `next=`, so quoting the whole thing nests the login URL into
        # itself and re-encodes it on every expired-auth bounce, exploding the
        # URL until the tab breaks. This guard runs in check_auth() (BEFORE
        # route handling), the actual source of the server-side loop.
        #
        # The login page is served ONLY at the public `/login` route (see
        # PUBLIC_PATHS + the routes.py `/login` handler); the app's client route
        # `/session/login` is NOT public, so a bare relative `login` from
        # `/session/login` resolves to `/session/login` again and re-triggers
        # check_auth() — an infinite redirect. Resolve to the real login route
        # with `../login`, which lands on `/login` from a `/session/*` scope and
        # on `<mount>/login` under a subpath mount (verified via urljoin). Carry
        # through only a validated, non-login inner `next` so a legitimate
        # post-login destination still survives a bounce that happened to land
        # on the login page.
        _login_path = (parsed.path or '/').rstrip('/')
        if _login_path == '/login' or _login_path.endswith('/login'):
            # /login itself is public → check_auth never redirects it; this only
            # fires for the non-public client login route (e.g. /session/login).
            _target = '../login' if '/' in _login_path.lstrip('/') else 'login'
            _inner = _safe_login_inner_next(parsed.query)
            if _inner:
                _target += '?next=' + _urlparse.quote(_inner, safe='/')
            handler.send_header('Location', _target)
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return False
        _path_with_query = parsed.path or '/'
        if parsed.query:
            _path_with_query += '?' + parsed.query
        # safe='/' keeps path separators readable; everything else (including
        # `?`, `&`, `=`) gets percent-encoded.
        _next = _urlparse.quote(_path_with_query, safe='/')
        handler.send_header('Location', 'login?next=' + _next)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
    return False


def _is_loopback(addr: str) -> bool:
    """Return True if *addr* is a loopback address (127.x.x.x, ::1, or ::ffff:127.x.x.x)."""
    import ipaddress as _ipaddress
    try:
        ip = _ipaddress.ip_address(addr)
        if ip.is_loopback:
            return True
        # Python < 3.12: is_loopback is False for ::ffff:127.x.x.x (gh-117566)
        if hasattr(ip, 'ipv4_mapped') and ip.ipv4_mapped is not None:
            return ip.ipv4_mapped.is_loopback
        return False
    except ValueError:
        return False


def _is_secure_context(handler=None) -> bool:
    """Return True if cookies should carry the Secure flag.

    Priority order:
    1. ``HERMES_WEBUI_SECURE`` env var: 1/true/yes -> True; 0/false/no -> False.
    2. Direct TLS socket (handler.request.getpeercert present) -> True.
    3. ``HERMES_WEBUI_TRUST_FORWARDED_PROTO=1`` opt-in: trust
       ``X-Forwarded-Proto: https`` header from a known reverse proxy.
    4. Otherwise -> False (loopback or non-loopback, plain HTTP is not secure).

    .. warning::
       ``X-Forwarded-Proto`` is only trustworthy behind a reverse proxy.
       It is ignored unless ``HERMES_WEBUI_TRUST_FORWARDED_PROTO=1`` is
       set explicitly, preventing header-injection attacks on plain-HTTP
       deployments.
    """
    env = os.getenv('HERMES_WEBUI_SECURE', '').strip().lower()
    if env in ('1', 'true', 'yes'):
        return True
    if env in ('0', 'false', 'no'):
        return False
    if handler is not None:
        if getattr(handler.request, 'getpeercert', None) is not None:
            return True
        trust_fwd = os.getenv('HERMES_WEBUI_TRUST_FORWARDED_PROTO', '').strip().lower()
        if trust_fwd in ('1', 'true', 'yes'):
            if handler.headers.get('X-Forwarded-Proto', '') == 'https':
                return True
    return False


def set_auth_cookie(handler, cookie_value) -> None:
    """Set the auth cookie on the response."""
    handler.send_header('Set-Cookie', _auth_cookie_header(cookie_value, handler))


def clear_auth_cookie(handler) -> None:
    """Clear the auth cookie on the response."""
    handler.send_header('Set-Cookie', _clear_auth_cookie_header())
