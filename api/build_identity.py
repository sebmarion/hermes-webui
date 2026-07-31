"""Embedded immutable-build identity for WebUI health and cutover proof."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import re
import sys
import threading
import time


_REQUIRED_MANAGED_ENV_KEYS = (
    "HERMES_WEBUI_RELEASE_ROOT",
    "HERMES_WEBUI_RELEASE_PATH",
    "HERMES_WEBUI_MANIFEST_SHA256",
    "HERMES_WEBUI_SELECTOR_GENERATION",
    "HERMES_WEBUI_SELECTOR_PATH",
    "HERMES_WEBUI_SELECTOR_STATE",
    "HERMES_WEBUI_SELECTOR_LOCK",
    "HERMES_WEBUI_LAUNCHD_LABEL",
    "HERMES_WEBUI_INTERPRETER_PATH",
    "HERMES_WEBUI_LAUNCH_MODE",
    "HERMES_WEBUI_AGENT_DIR",
    "HERMES_WEBUI_AGENT_COMMIT",
    "HERMES_WEBUI_AGENT_TREE",
    "HERMES_WEBUI_AGENT_MANIFEST_PATH",
    "HERMES_WEBUI_AGENT_MANIFEST_SHA256",
    "HERMES_WEBUI_RUNTIME_PATH",
    "HERMES_WEBUI_RUNTIME_PYTHON_HOME",
    "HERMES_WEBUI_RUNTIME_SITE_PACKAGES",
    "HERMES_WEBUI_RUNTIME_MANIFEST_PATH",
    "HERMES_WEBUI_RUNTIME_MANIFEST_SHA256",
)
_OPTIONAL_MANAGED_ENV_KEYS = (
    "HERMES_WEBUI_STARTUP_FENCED",
    "HERMES_WEBUI_STARTUP_TRANSACTION_ID",
)
MANAGED_ENV_KEYS = _REQUIRED_MANAGED_ENV_KEYS + _OPTIONAL_MANAGED_ENV_KEYS
_WEBUI_RELEASE_ENV_KEYS = (
    "HERMES_WEBUI_RELEASE_ROOT",
    "HERMES_WEBUI_RELEASE_PATH",
    "HERMES_WEBUI_MANIFEST_SHA256",
    "HERMES_WEBUI_SELECTOR_GENERATION",
    "HERMES_WEBUI_SELECTOR_PATH",
)
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_LAUNCHD_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ATTESTATION_TTL_SECONDS = 30.0

_CACHE_LOCK = threading.Lock()
_VERIFY_LOCK = threading.Lock()
_CACHED_ENV_SIGNATURE: tuple[str | None, ...] | None = None
_CACHED_IDENTITY: dict | None = None
_CACHED_VERIFIED_AT: float | None = None
_CACHED_MONOTONIC: float | None = None
_REFRESH_IN_PROGRESS = False


def _running_code_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _running_interpreter() -> Path:
    return Path(sys.executable).absolute()


def _managed_environment() -> tuple[dict[str, str] | None, str | None]:
    values = {key: os.environ.get(key) for key in MANAGED_ENV_KEYS}
    release_present = [
        key for key in _WEBUI_RELEASE_ENV_KEYS if values[key] is not None
    ]
    # HERMES_WEBUI_AGENT_DIR is also a supported unmanaged development setting;
    # only WebUI release identity activates the paired managed-build contract.
    if not release_present:
        return None, None
    missing_required = [
        key for key in _REQUIRED_MANAGED_ENV_KEYS if values[key] is None
    ]
    if missing_required:
        return None, "incomplete_managed_environment"
    if any(
        not str(values[key]).strip()
        for key in _REQUIRED_MANAGED_ENV_KEYS
    ):
        return None, "empty_managed_environment"
    startup_values = [values[key] for key in _OPTIONAL_MANAGED_ENV_KEYS]
    if any(value is not None for value in startup_values):
        if any(value is None or not str(value).strip() for value in startup_values):
            return None, "incomplete_startup_fence_environment"
        if (
            str(values["HERMES_WEBUI_STARTUP_FENCED"]) != "1"
            or not _TRANSACTION_ID.fullmatch(
                str(values["HERMES_WEBUI_STARTUP_TRANSACTION_ID"])
            )
        ):
            return None, "invalid_startup_fence_environment"
    return {
        key: str(value)
        for key, value in values.items()
        if value is not None
    }, None


def _compute_identity() -> dict:
    managed, environment_error = _managed_environment()
    if managed is None and environment_error is None:
        return {"status": "unmanaged", "valid": False}
    if managed is None:
        return {
            "status": "invalid",
            "valid": False,
            "error_code": environment_error,
        }
    try:
        from scripts.webui_release_selector import SelectorError, verify_release

        generation = int(managed["HERMES_WEBUI_SELECTOR_GENERATION"])
        if generation < 0:
            raise ValueError("negative generation")
        release_path = Path(managed["HERMES_WEBUI_RELEASE_PATH"])
        code_root = _running_code_root()
        if release_path.resolve(strict=True) != code_root:
            raise SelectorError("running code root does not match selected release")
        launch_mode = managed["HERMES_WEBUI_LAUNCH_MODE"]
        if launch_mode not in {"selector", "direct-fallback"}:
            raise SelectorError("managed launch mode is unsupported")
        selector_state_path = Path(managed["HERMES_WEBUI_SELECTOR_STATE"])
        selector_lock_path = Path(managed["HERMES_WEBUI_SELECTOR_LOCK"])
        if (
            not selector_state_path.is_absolute()
            or not selector_lock_path.is_absolute()
            or Path(os.path.abspath(selector_state_path)) != selector_state_path
            or Path(os.path.abspath(selector_lock_path)) != selector_lock_path
            or selector_state_path.parent != selector_lock_path.parent
        ):
            raise SelectorError("managed selector control paths are invalid")
        launchd_label = managed["HERMES_WEBUI_LAUNCHD_LABEL"]
        if not _LAUNCHD_LABEL.fullmatch(launchd_label):
            raise SelectorError("managed launchd label is invalid")
        verified = verify_release(
            release_path,
            release_root=managed["HERMES_WEBUI_RELEASE_ROOT"],
            expected_manifest_sha256=managed["HERMES_WEBUI_MANIFEST_SHA256"],
            selector_path=managed["HERMES_WEBUI_SELECTOR_PATH"],
            verify_selector_identity=launch_mode == "selector",
        )
        configured_interpreter = Path(managed["HERMES_WEBUI_INTERPRETER_PATH"])
        verified_interpreter = Path(verified["interpreter_resolved_path"])
        if configured_interpreter.resolve(strict=True) != verified_interpreter:
            raise SelectorError("running interpreter identity does not match manifest")
        if _running_interpreter() != configured_interpreter.absolute():
            raise SelectorError("process executable does not match manifest")
        agent_environment = {
            "agent_source_path": managed["HERMES_WEBUI_AGENT_DIR"],
            "agent_source_commit": managed["HERMES_WEBUI_AGENT_COMMIT"],
            "agent_source_tree": managed["HERMES_WEBUI_AGENT_TREE"],
            "agent_source_manifest_path": managed[
                "HERMES_WEBUI_AGENT_MANIFEST_PATH"
            ],
            "agent_source_manifest_sha256": managed[
                "HERMES_WEBUI_AGENT_MANIFEST_SHA256"
            ],
        }
        for key, value in agent_environment.items():
            if value != str(verified.get(key) or ""):
                raise SelectorError(
                    f"running agent source environment does not match manifest: {key}"
                )
        runtime_environment = {
            "runtime_path": managed["HERMES_WEBUI_RUNTIME_PATH"],
            "runtime_python_home_path": managed[
                "HERMES_WEBUI_RUNTIME_PYTHON_HOME"
            ],
            "runtime_site_packages_path": managed[
                "HERMES_WEBUI_RUNTIME_SITE_PACKAGES"
            ],
            "runtime_manifest_path": managed[
                "HERMES_WEBUI_RUNTIME_MANIFEST_PATH"
            ],
            "runtime_manifest_sha256": managed[
                "HERMES_WEBUI_RUNTIME_MANIFEST_SHA256"
            ],
        }
        for key, value in runtime_environment.items():
            if value != str(verified.get(key) or ""):
                raise SelectorError(
                    f"running runtime environment does not match manifest: {key}"
                )
        agent_root = Path(verified["agent_source_resolved_path"])
        from api import config as api_config

        configured_agent_root = Path(api_config._AGENT_DIR).resolve(strict=True)
        if configured_agent_root != agent_root:
            raise SelectorError("api.config Agent root does not match manifest")
        expected_agent_modules = {
            "run_agent": ("run_agent.py",),
            "agent": ("agent",),
            "hermes_cli": ("hermes_cli",),
            "tools": ("tools",),
            "tools.process_registry": ("tools", "process_registry.py"),
        }
        for module_name, expected_parts in expected_agent_modules.items():
            module = importlib.import_module(module_name)
            imported_path = Path(module.__file__).resolve(strict=True)
            if not imported_path.is_file():
                raise SelectorError(
                    f"imported {module_name} module is not a file"
                )
            try:
                relative = imported_path.relative_to(agent_root)
            except ValueError as exc:
                raise SelectorError(
                    f"imported {module_name} module is outside the attested Agent root"
                ) from exc
            if expected_parts == ("run_agent.py",):
                if relative.parts != expected_parts:
                    raise SelectorError("imported run_agent module path is invalid")
            elif relative.parts[: len(expected_parts)] != expected_parts:
                raise SelectorError(
                    f"imported {module_name} module path is invalid"
                )
        for module_name, module in (
            ("api.build_identity", sys.modules[__name__]),
            ("api.config", api_config),
        ):
            module_path = Path(module.__file__).resolve(strict=True)
            try:
                module_path.relative_to(code_root)
            except ValueError as exc:
                raise SelectorError(
                    f"imported {module_name} module is outside the attested WebUI root"
                ) from exc
        critical_webui_modules = (
            "api.routes",
            "api.release_control",
            "api.streaming",
            "server",
        )
        for module_name in critical_webui_modules:
            module = sys.modules.get(module_name)
            module_file = getattr(module, "__file__", None) if module else None
            if module_file is None:
                continue
            module_path = Path(module_file).resolve(strict=True)
            try:
                module_path.relative_to(code_root)
            except ValueError as exc:
                raise SelectorError(
                    f"imported {module_name} module is outside the attested WebUI root"
                ) from exc
    except (AttributeError, ImportError, OSError, TypeError, ValueError, RuntimeError):
        return {
            "status": "invalid",
            "valid": False,
            "error_code": "manifest_verification_failed",
        }
    return {
        "status": "managed",
        "valid": True,
        "build_id": verified["build_id"],
        "commit": verified["commit"],
        "tree": verified["tree"],
        "manifest_sha256": verified["manifest_sha256"],
        "agent_commit": verified["agent_source_commit"],
        "agent_tree": verified["agent_source_tree"],
        "agent_manifest_sha256": verified["agent_source_manifest_sha256"],
        "runtime_manifest_sha256": verified["runtime_manifest_sha256"],
        "selector_generation": generation,
        "release_path": verified["release_path"],
        "launch_mode": launch_mode,
        "selector_verified": verified["selector_verified"],
        "selector_state_path": str(selector_state_path),
        "selector_lock_path": str(selector_lock_path),
        "launchd_label": launchd_label,
        "startup_fenced": managed.get("HERMES_WEBUI_STARTUP_FENCED") == "1",
        "startup_transaction_id": managed.get(
            "HERMES_WEBUI_STARTUP_TRANSACTION_ID"
        ),
    }


def _decorate_cached(identity: dict, *, refreshing: bool = False) -> dict:
    result = dict(identity)
    if result.get("status") in {"managed", "invalid"} and _CACHED_VERIFIED_AT is not None:
        result["verified_at"] = _CACHED_VERIFIED_AT
        age = max(0.0, time.monotonic() - float(_CACHED_MONOTONIC or 0.0))
        result["verification_age_seconds"] = round(age, 3)
        result["attestation"] = "refreshing" if refreshing else "fresh"
    return result


def _refresh_identity(signature: tuple[str | None, ...]) -> dict:
    global _CACHED_ENV_SIGNATURE, _CACHED_IDENTITY
    global _CACHED_VERIFIED_AT, _CACHED_MONOTONIC
    with _VERIFY_LOCK:
        identity = _compute_identity()
        with _CACHE_LOCK:
            current_signature = tuple(os.environ.get(key) for key in MANAGED_ENV_KEYS)
            if current_signature == signature:
                _CACHED_ENV_SIGNATURE = signature
                _CACHED_IDENTITY = dict(identity)
                _CACHED_VERIFIED_AT = time.time()
                _CACHED_MONOTONIC = time.monotonic()
            return _decorate_cached(identity)


def _refresh_identity_in_background(signature: tuple[str | None, ...]) -> None:
    global _REFRESH_IN_PROGRESS
    try:
        _refresh_identity(signature)
    finally:
        with _CACHE_LOCK:
            _REFRESH_IN_PROGRESS = False


def get_build_identity(*, refresh: bool = False) -> dict:
    """Return bounded single-flight build attestation for `/health`."""
    global _REFRESH_IN_PROGRESS
    signature = tuple(os.environ.get(key) for key in MANAGED_ENV_KEYS)
    start_background = False
    with _CACHE_LOCK:
        if not refresh and _CACHED_IDENTITY is not None and _CACHED_ENV_SIGNATURE == signature:
            age = max(0.0, time.monotonic() - float(_CACHED_MONOTONIC or 0.0))
            if age >= ATTESTATION_TTL_SECONDS and not _REFRESH_IN_PROGRESS:
                _REFRESH_IN_PROGRESS = True
                start_background = True
            result = _decorate_cached(
                _CACHED_IDENTITY,
                refreshing=_REFRESH_IN_PROGRESS,
            )
        else:
            result = None
    if start_background:
        threading.Thread(
            target=_refresh_identity_in_background,
            args=(signature,),
            name="webui-build-attestation",
            daemon=True,
        ).start()
    if result is not None:
        return result
    return _refresh_identity(signature)
