"""Release 0B immutable selector, build identity, and cutover contracts."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from api import build_identity
from scripts import webui_release_cutover as cutover
from scripts import webui_release_selector as selector


@pytest.fixture(autouse=True)
def _restore_immutable_tmp_permissions(tmp_path):
    """Make fixture-owned immutable release trees removable by pytest."""
    yield
    for root, directories, filenames in os.walk(tmp_path, topdown=False):
        root_path = Path(root)
        for filename in filenames:
            try:
                (root_path / filename).chmod(0o600)
            except FileNotFoundError:
                pass
        for directory in directories:
            try:
                (root_path / directory).chmod(0o700)
            except FileNotFoundError:
                pass
        try:
            root_path.chmod(0o700)
        except FileNotFoundError:
            pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cutover_script_is_directly_executable_from_outside_repo(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(Path(cutover.__file__).resolve()), "--help"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Hermes WebUI immutable cutover driver" in completed.stdout


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _chmod(path: Path, mode: int) -> None:
    path.chmod(mode)


def _identity_receipt(path: Path) -> dict[str, str]:
    return {
        "path": str(path),
        "resolved_path": str(path.resolve()),
        "sha256": _sha(path.resolve()),
    }


def _release_metadata(changed_files: set[str]) -> dict:
    return {
        "patch_decisions": {
            path: {"decision": "ship", "rationale": "covered by release test"}
            for path in sorted(changed_files)
        },
        "test_receipts": [
            {
                "name": "release-selector-focused",
                "status": "passed",
                "receipt_sha256": "c" * 64,
            }
        ],
        "artifact_hashes": {"preserved_worktree_patch": "d" * 64},
    }


def _agent_source_snapshot(tmp_path: Path) -> dict:
    contents = {
        "agent/__init__.py": b"# immutable agent package\n",
        "hermes_cli/__init__.py": b"# immutable cli package\n",
        "run_agent.py": b"def main():\n    return 0\n",
        "tools/__init__.py": b"# immutable tools package\n",
        "tools/process_registry.py": b"PROCESS_REGISTRY = True\n",
    }
    commit = "e" * 40
    tree = "f" * 40
    manifest = {
        "version": 1,
        "origin_url": "git@github.com:NousResearch/hermes-agent.git",
        "base_commit": "d" * 40,
        "commit": commit,
        "tree": tree,
        "changed_files": [],
        "files": {
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in sorted(contents.items())
        },
    }
    encoded_manifest = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest_sha256 = hashlib.sha256(encoded_manifest).hexdigest()
    release_root = tmp_path / "agent-releases"
    snapshots_root = release_root / "snapshots"
    manifests_root = release_root / "manifests"
    source_path = snapshots_root / manifest_sha256
    manifest_path = manifests_root / f"{manifest_sha256}.json"
    snapshots_root.mkdir(parents=True, exist_ok=True)
    manifests_root.mkdir(parents=True, exist_ok=True)
    if not source_path.exists():
        source_path.mkdir()
        for relative, content in contents.items():
            destination = source_path / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            _chmod(destination, 0o444)
        for directory in sorted(
            (path for path in source_path.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            _chmod(directory, 0o555)
        _chmod(source_path, 0o555)
    if not manifest_path.exists():
        manifest_path.write_bytes(encoded_manifest)
        _chmod(manifest_path, 0o444)
    identity = {
        "path": str(source_path),
        "resolved_path": str(source_path.resolve()),
        "commit": commit,
        "tree": tree,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
    }
    return {
        "identity": identity,
        "release_root": release_root,
        "source_path": source_path,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "manifest_sha256": manifest_sha256,
    }


def _rewrite_release_manifest(release: dict, manifest: dict) -> str:
    _chmod(release["manifest_path"], 0o644)
    release["manifest_path"].write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _chmod(release["manifest_path"], 0o444)
    release["manifest"] = manifest
    release["manifest_sha256"] = _sha(release["manifest_path"])
    return release["manifest_sha256"]


def _attest_runtime_agent(
    monkeypatch,
    release: dict,
    *,
    agent_dir: Path | None = None,
    run_agent_file: Path | None = None,
    module_files: dict[str, Path] | None = None,
) -> None:
    from api import config as api_config

    source_path = release["agent_source"]["source_path"]
    monkeypatch.setattr(api_config, "_AGENT_DIR", agent_dir or source_path)
    monkeypatch.setattr(
        api_config,
        "__file__",
        str(release["release_path"] / "api" / "config.py"),
    )
    monkeypatch.setattr(
        build_identity,
        "__file__",
        str(release["release_path"] / "api" / "build_identity.py"),
    )
    critical_webui_modules = {
        "api.routes": release["release_path"] / "api" / "routes.py",
        "api.release_control": release["release_path"] / "api" / "release_control.py",
        "api.streaming": release["release_path"] / "api" / "streaming.py",
        "server": release["release_path"] / "server.py",
    }
    for module_name, module_path in critical_webui_modules.items():
        module = sys.modules.get(module_name)
        if module is not None:
            monkeypatch.setattr(module, "__file__", str(module_path))
    monkeypatch.setitem(
        sys.modules,
        "run_agent",
        SimpleNamespace(__file__=str(run_agent_file or (source_path / "run_agent.py"))),
    )
    expected_modules = {
        "agent": source_path / "agent" / "__init__.py",
        "hermes_cli": source_path / "hermes_cli" / "__init__.py",
        "tools": source_path / "tools" / "__init__.py",
        "tools.process_registry": source_path / "tools" / "process_registry.py",
    }
    expected_modules.update(module_files or {})
    for module_name, module_path in expected_modules.items():
        monkeypatch.setitem(
            sys.modules,
            module_name,
            SimpleNamespace(__file__=str(module_path)),
        )


def _runtime_snapshot(tmp_path: Path) -> dict:
    contents = {
        "python-home/bin/python3.11": b"#!/bin/sh\nexit 0\n",
        "python-home/lib/python3.11/os.py": b"# sealed stdlib\n",
        "site-packages/yaml/__init__.py": b"VALUE = 1\n",
    }
    manifest = {
        "version": 1,
        "interpreter_relative_path": "python-home/bin/python3.11",
        "site_packages_relative_path": "site-packages",
        "files": {
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in sorted(contents.items())
        },
    }
    encoded = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest_sha256 = hashlib.sha256(encoded).hexdigest()
    root = tmp_path / "runtime-releases"
    runtime_path = root / "snapshots" / manifest_sha256
    manifest_path = root / "manifests" / f"{manifest_sha256}.json"
    if not runtime_path.exists():
        for relative, content in contents.items():
            destination = runtime_path / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            _chmod(
                destination,
                0o555 if relative == "python-home/bin/python3.11" else 0o444,
            )
        for directory in sorted(
            (path for path in runtime_path.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            _chmod(directory, 0o555)
        _chmod(runtime_path, 0o555)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if not manifest_path.exists():
        manifest_path.write_bytes(encoded)
        _chmod(manifest_path, 0o444)
    interpreter = runtime_path / "python-home" / "bin" / "python3.11"
    identity = {
        "path": str(runtime_path),
        "resolved_path": str(runtime_path.resolve()),
        "python_home_path": str(runtime_path / "python-home"),
        "site_packages_path": str(runtime_path / "site-packages"),
        "interpreter_path": str(interpreter),
        "interpreter_resolved_path": str(interpreter.resolve()),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
    }
    return {"identity": identity, "manifest": manifest, "path": runtime_path}


def _sealed_runtime_for_build(tmp_path: Path) -> tuple[dict, Path]:
    identity = _runtime_snapshot(tmp_path)["identity"]
    return identity, Path(identity["interpreter_path"])


def _managed_release(
    tmp_path: Path,
    build_id: str = "build-1",
    *,
    symlink_interpreter: bool = False,
) -> dict:
    agent_source = _agent_source_snapshot(tmp_path)
    runtime = _runtime_snapshot(tmp_path)
    selector_path = tmp_path / "bin" / "webui-release-selector.py"
    interpreter_path = Path(runtime["identity"]["interpreter_path"])
    _write(selector_path, "# trusted selector\n")
    _chmod(selector_path, 0o755)

    release_root = tmp_path / "releases"
    release_path = release_root / build_id
    _write(release_path / "bootstrap.py", "print('boot')\n")
    _write(release_path / "api" / "app.py", "VALUE = 1\n")
    _write(release_path / "api" / "build_identity.py", "# build identity\n")
    _write(release_path / "api" / "config.py", "# config\n")
    _write(release_path / "api" / "routes.py", "# routes\n")
    _write(release_path / "api" / "release_control.py", "# release control\n")
    _write(release_path / "api" / "streaming.py", "# streaming\n")
    _write(release_path / "server.py", "# server\n")
    files = {
        "api/app.py": _sha(release_path / "api" / "app.py"),
        "api/build_identity.py": _sha(release_path / "api" / "build_identity.py"),
        "api/config.py": _sha(release_path / "api" / "config.py"),
        "api/routes.py": _sha(release_path / "api" / "routes.py"),
        "api/release_control.py": _sha(
            release_path / "api" / "release_control.py"
        ),
        "api/streaming.py": _sha(release_path / "api" / "streaming.py"),
        "bootstrap.py": _sha(release_path / "bootstrap.py"),
        "server.py": _sha(release_path / "server.py"),
    }
    manifest = {
        "version": 1,
        "build_id": build_id,
        "origin_url": "git@github.com:nesquena/hermes-webui.git",
        "base_commit": "c" * 40,
        "commit": "a" * 40,
        "tree": "b" * 40,
        "changed_files": ["api/app.py"],
        **_release_metadata({"api/app.py"}),
        "files": files,
        "selector": {
            "path": str(selector_path),
            "resolved_path": str(selector_path.resolve()),
            "sha256": _sha(selector_path),
        },
        "interpreter": {
            "path": str(interpreter_path),
            "resolved_path": str(interpreter_path.resolve()),
            "sha256": _sha(interpreter_path),
        },
        "runtime": runtime["identity"],
        "agent_source": agent_source["identity"],
    }
    manifest_path = release_path / selector.MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    for file_path in release_path.rglob("*"):
        if file_path.is_file():
            _chmod(file_path, 0o444)
    for directory in sorted(
        (path for path in release_path.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        _chmod(directory, 0o555)
    _chmod(release_path, 0o555)
    _chmod(release_root, 0o755)
    return {
        "build_id": build_id,
        "selector_path": selector_path,
        "interpreter_path": interpreter_path,
        "release_root": release_root,
        "release_path": release_path,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "manifest_sha256": _sha(manifest_path),
        "agent_source": agent_source,
        "runtime": runtime,
        "record": {
            "manifest_sha256": _sha(manifest_path),
            "commit": manifest["commit"],
            "tree": manifest["tree"],
        },
    }


def _managed_env(release: dict, generation: int = 7) -> dict[str, str]:
    agent_identity = release["agent_source"]["identity"]
    return {
        "HERMES_WEBUI_RELEASE_ROOT": str(release["release_root"]),
        "HERMES_WEBUI_RELEASE_PATH": str(release["release_path"]),
        "HERMES_WEBUI_MANIFEST_SHA256": release["manifest_sha256"],
        "HERMES_WEBUI_SELECTOR_GENERATION": str(generation),
        "HERMES_WEBUI_SELECTOR_PATH": str(release["selector_path"]),
        "HERMES_WEBUI_SELECTOR_STATE": str(release["release_root"].parent / "selector.json"),
        "HERMES_WEBUI_SELECTOR_LOCK": str(release["release_root"].parent / "selector.lock"),
        "HERMES_WEBUI_LAUNCHD_LABEL": "com.example.hermes-webui",
        "HERMES_WEBUI_INTERPRETER_PATH": str(release["interpreter_path"]),
        "HERMES_WEBUI_LAUNCH_MODE": "selector",
        "HERMES_WEBUI_AGENT_DIR": agent_identity["path"],
        "HERMES_WEBUI_AGENT_COMMIT": agent_identity["commit"],
        "HERMES_WEBUI_AGENT_TREE": agent_identity["tree"],
        "HERMES_WEBUI_AGENT_MANIFEST_PATH": agent_identity["manifest_path"],
        "HERMES_WEBUI_AGENT_MANIFEST_SHA256": agent_identity["manifest_sha256"],
        "HERMES_WEBUI_RUNTIME_PATH": release["runtime"]["identity"]["path"],
        "HERMES_WEBUI_RUNTIME_PYTHON_HOME": release["runtime"]["identity"][
            "python_home_path"
        ],
        "HERMES_WEBUI_RUNTIME_SITE_PACKAGES": release["runtime"]["identity"][
            "site_packages_path"
        ],
        "HERMES_WEBUI_RUNTIME_MANIFEST_PATH": release["runtime"]["identity"][
            "manifest_path"
        ],
        "HERMES_WEBUI_RUNTIME_MANIFEST_SHA256": release["runtime"]["identity"][
            "manifest_sha256"
        ],
    }


def test_complete_manifest_and_external_identities_verify(tmp_path):
    release = _managed_release(tmp_path)

    verified = selector.verify_release(
        release["release_path"],
        release_root=release["release_root"],
        expected_manifest_sha256=release["manifest_sha256"],
        selector_path=release["selector_path"],
    )

    assert verified["build_id"] == "build-1"
    assert verified["commit"] == "a" * 40
    assert verified["tree"] == "b" * 40
    assert verified["interpreter_path"] == str(release["interpreter_path"].resolve())
    assert verified["agent_source_path"] == str(
        release["agent_source"]["source_path"]
    )
    assert verified["agent_source_commit"] == "e" * 40
    assert verified["agent_source_tree"] == "f" * 40
    assert verified["agent_source_manifest_sha256"] == release["agent_source"][
        "manifest_sha256"
    ]


def test_candidate_identity_match_projects_full_release_to_signed_process(tmp_path):
    release = _managed_release(tmp_path)
    transaction_id = "candidate-projection-transaction-000001"
    verified = selector.verify_release(
        release["release_path"],
        release_root=release["release_root"],
        expected_manifest_sha256=release["manifest_sha256"],
        selector_path=release["selector_path"],
    )
    expected = {
        **verified,
        "selector_generation": 2,
        "launch_mode": "selector",
        "selector_state_path": str(tmp_path / "control" / "selector.json"),
        "selector_lock_path": str(tmp_path / "control" / "selector.lock"),
        "launchd_label": "com.example.hermes-webui",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    actual = {
        "build_id": verified["build_id"],
        "commit": verified["commit"],
        "tree": verified["tree"],
        "manifest_sha256": verified["manifest_sha256"],
        "agent_commit": verified["agent_source_commit"],
        "agent_tree": verified["agent_source_tree"],
        "agent_manifest_sha256": verified["agent_source_manifest_sha256"],
        "runtime_manifest_sha256": verified["runtime_manifest_sha256"],
        "selector_generation": 2,
        "release_path": verified["release_path"],
        "launch_mode": "selector",
        "selector_verified": True,
        "selector_state_path": str(tmp_path / "control" / "selector.json"),
        "selector_lock_path": str(tmp_path / "control" / "selector.lock"),
        "launchd_label": "com.example.hermes-webui",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }

    assert cutover._candidate_identity_matches(actual, expected)

    actual["agent_manifest_sha256"] = "0" * 64
    assert not cutover._candidate_identity_matches(actual, expected)


def test_expected_release_identity_is_reverified_before_cutover(tmp_path):
    release = _managed_release(tmp_path)
    verified = selector.verify_release(
        release["release_path"],
        release_root=release["release_root"],
        expected_manifest_sha256=release["manifest_sha256"],
        selector_path=release["selector_path"],
    )

    assert (
        cutover._attest_expected_release_identity(
            verified,
            selector_path=str(release["selector_path"]),
            label="candidate",
        )
        == verified
    )

    drifted = {
        **verified,
        "runtime_site_packages_path": str(tmp_path / "foreign-site-packages"),
    }
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="candidate release identity mismatch: runtime_site_packages_path",
    ):
        cutover._attest_expected_release_identity(
            drifted,
            selector_path=str(release["selector_path"]),
            label="candidate",
        )


def test_manifest_requires_paired_agent_source_identity(tmp_path):
    release = _managed_release(tmp_path)
    manifest = json.loads(release["manifest_path"].read_text(encoding="utf-8"))
    manifest.pop("agent_source")
    expected_hash = _rewrite_release_manifest(release, manifest)

    with pytest.raises(selector.SelectorError, match="agent source identity"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=expected_hash,
            selector_path=release["selector_path"],
        )


@pytest.mark.parametrize(
    "mutation",
    ["extra", "missing", "hash", "symlink", "writable-file", "writable-directory"],
)
def test_manifest_rejects_paired_agent_source_drift(tmp_path, mutation):
    release = _managed_release(tmp_path)
    source_path = release["agent_source"]["source_path"]
    run_agent = source_path / "run_agent.py"
    if mutation == "extra":
        _chmod(source_path, 0o755)
        _write(source_path / "unexpected.py", "EXTRA = True\n")
        _chmod(source_path / "unexpected.py", 0o444)
        _chmod(source_path, 0o555)
    elif mutation == "missing":
        _chmod(source_path, 0o755)
        run_agent.unlink()
        _chmod(source_path, 0o555)
    elif mutation == "hash":
        _chmod(run_agent, 0o644)
        _write(run_agent, "TAMPERED = True\n")
        _chmod(run_agent, 0o444)
    elif mutation == "symlink":
        _chmod(source_path, 0o755)
        run_agent.unlink()
        run_agent.symlink_to(source_path / "agent" / "__init__.py")
        _chmod(source_path, 0o555)
    elif mutation == "writable-file":
        _chmod(run_agent, 0o644)
    else:
        _chmod(source_path / "agent", 0o755)

    with pytest.raises(selector.SelectorError, match="agent source"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        )
    if mutation == "symlink":
        _chmod(source_path, 0o755)
        run_agent.unlink()
        _chmod(source_path, 0o555)


def test_manifest_rejects_paired_agent_manifest_tamper(tmp_path):
    release = _managed_release(tmp_path)
    manifest_path = release["agent_source"]["manifest_path"]
    _chmod(manifest_path, 0o644)
    manifest_path.write_text("{}\n", encoding="utf-8")
    _chmod(manifest_path, 0o444)

    with pytest.raises(selector.SelectorError, match="agent source manifest hash"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        )


def test_manifest_rejects_writable_paired_agent_manifest(tmp_path):
    release = _managed_release(tmp_path)
    _chmod(release["agent_source"]["manifest_path"], 0o644)

    with pytest.raises(selector.SelectorError, match="agent source manifest.*read-only"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        )


def test_manifest_rejects_noncanonical_paired_agent_path(tmp_path):
    release = _managed_release(tmp_path)
    source_path = release["agent_source"]["source_path"]
    noncanonical = source_path.parent / ".." / "snapshots" / source_path.name
    manifest = json.loads(release["manifest_path"].read_text(encoding="utf-8"))
    manifest["agent_source"]["path"] = str(noncanonical)
    expected_hash = _rewrite_release_manifest(release, manifest)

    with pytest.raises(selector.SelectorError, match="absolute and canonical"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=expected_hash,
            selector_path=release["selector_path"],
        )


def test_manifest_rejects_non_content_addressed_agent_root(tmp_path):
    release = _managed_release(tmp_path)
    source_path = release["agent_source"]["source_path"]
    renamed = source_path.with_name("not-the-manifest-digest")
    source_path.rename(renamed)
    manifest = json.loads(release["manifest_path"].read_text(encoding="utf-8"))
    manifest["agent_source"]["path"] = str(renamed)
    manifest["agent_source"]["resolved_path"] = str(renamed.resolve())
    expected_hash = _rewrite_release_manifest(release, manifest)

    with pytest.raises(selector.SelectorError, match="content-addressed"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=expected_hash,
            selector_path=release["selector_path"],
        )


@pytest.mark.parametrize("mutation", ["extra", "missing", "hash"])
def test_manifest_rejects_file_set_and_hash_drift(tmp_path, mutation):
    release = _managed_release(tmp_path)
    if mutation == "extra":
        _chmod(release["release_path"], 0o755)
        _write(release["release_path"] / "unexpected.txt", "extra\n")
        _chmod(release["release_path"] / "unexpected.txt", 0o444)
        _chmod(release["release_path"], 0o555)
    elif mutation == "missing":
        _chmod(release["release_path"] / "api", 0o755)
        (release["release_path"] / "api" / "app.py").unlink()
        _chmod(release["release_path"] / "api", 0o555)
    else:
        _chmod(release["release_path"] / "api" / "app.py", 0o644)
        _write(release["release_path"] / "api" / "app.py", "tampered\n")
        _chmod(release["release_path"] / "api" / "app.py", 0o444)

    with pytest.raises(selector.SelectorError, match=mutation):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        )


@pytest.mark.parametrize("target", ["selector", "interpreter"])
def test_manifest_rejects_external_identity_drift(tmp_path, target):
    release = _managed_release(tmp_path)
    if target == "interpreter":
        _chmod(release[f"{target}_path"], 0o755)
    _write(release[f"{target}_path"], "tampered external binary\n")

    with pytest.raises(selector.SelectorError, match=target):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        )


def test_manifest_rejects_root_escape_symlinks_and_traversal(tmp_path):
    release = _managed_release(tmp_path)
    outside = tmp_path / "outside"
    _write(outside / "bootstrap.py", "outside\n")

    with pytest.raises(selector.SelectorError, match="root"):
        selector.verify_release(
            outside,
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        )

    symlinked_release = release["release_root"] / "linked"
    symlinked_release.symlink_to(release["release_path"], target_is_directory=True)
    with pytest.raises(selector.SelectorError, match="symlink"):
        selector.verify_release(
            symlinked_release,
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        )
    symlinked_release.unlink()
    manifest = json.loads(release["manifest_path"].read_text(encoding="utf-8"))
    manifest["files"]["../escape.py"] = "0" * 64
    _chmod(release["manifest_path"], 0o644)
    release["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")
    _chmod(release["manifest_path"], 0o444)
    with pytest.raises(selector.SelectorError, match="path"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=_sha(release["manifest_path"]),
            selector_path=release["selector_path"],
        )


def test_manifest_rejects_symlinked_content(tmp_path):
    release = _managed_release(tmp_path)
    app_path = release["release_path"] / "api" / "app.py"
    _chmod(app_path.parent, 0o755)
    app_path.unlink()
    app_path.symlink_to(release["release_path"] / "bootstrap.py")
    _chmod(app_path.parent, 0o555)
    manifest = json.loads(release["manifest_path"].read_text(encoding="utf-8"))
    manifest["files"]["api/app.py"] = _sha(app_path)
    _chmod(release["manifest_path"], 0o644)
    release["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")
    _chmod(release["manifest_path"], 0o444)

    with pytest.raises(selector.SelectorError, match="symlink"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=_sha(release["manifest_path"]),
            selector_path=release["selector_path"],
        )
    _chmod(app_path.parent, 0o755)
    app_path.unlink()


def test_manifest_rejects_selector_invocation_alias(tmp_path):
    release = _managed_release(tmp_path)
    alias = release["selector_path"].with_name("selector-alias.py")
    alias.symlink_to(release["selector_path"])

    with pytest.raises(selector.SelectorError, match="invocation path"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=alias,
        )


@pytest.mark.parametrize("length", [39, 41, 63, 65])
def test_selector_rejects_noncanonical_git_object_id_lengths(tmp_path, length):
    release = _managed_release(tmp_path)
    manifest = json.loads(release["manifest_path"].read_text(encoding="utf-8"))
    manifest["commit"] = "a" * length
    _chmod(release["manifest_path"], 0o644)
    release["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")
    _chmod(release["manifest_path"], 0o444)

    with pytest.raises(selector.SelectorError, match="commit or tree"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=_sha(release["manifest_path"]),
            selector_path=release["selector_path"],
        )


def test_release_0b_manifest_requires_admission_receipts(tmp_path):
    release = _managed_release(tmp_path)
    manifest = json.loads(release["manifest_path"].read_text(encoding="utf-8"))
    manifest.pop("artifact_hashes")
    _chmod(release["manifest_path"], 0o644)
    release["manifest_path"].write_text(json.dumps(manifest), encoding="utf-8")
    _chmod(release["manifest_path"], 0o444)

    with pytest.raises(selector.SelectorError, match="artifact hashes"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=_sha(release["manifest_path"]),
            selector_path=release["selector_path"],
        )


def test_manifest_rejects_writable_release_leaf_and_unsafe_release_root(tmp_path):
    release = _managed_release(tmp_path)
    _chmod(release["release_path"] / "bootstrap.py", 0o644)

    with pytest.raises(selector.SelectorError, match="read-only"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        )

    _chmod(release["release_path"] / "bootstrap.py", 0o444)
    _chmod(release["release_root"], 0o777)
    with pytest.raises(selector.SelectorError, match="ownership or mode"):
        selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        )


def test_build_identity_managed_unmanaged_and_invalid(monkeypatch, tmp_path):
    for key in build_identity.MANAGED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    assert build_identity.get_build_identity(refresh=True) == {
        "status": "unmanaged",
        "valid": False,
    }

    release = _managed_release(tmp_path)
    _attest_runtime_agent(monkeypatch, release)
    for key, value in _managed_env(release).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        build_identity, "_running_code_root", lambda: release["release_path"]
    )
    monkeypatch.setattr(
        build_identity,
        "_running_interpreter",
        lambda: release["interpreter_path"].resolve(),
    )
    managed = build_identity.get_build_identity(refresh=True)
    assert {
        key: managed[key]
        for key in (
            "status",
            "valid",
            "build_id",
            "commit",
            "tree",
            "manifest_sha256",
            "agent_commit",
            "agent_tree",
            "agent_manifest_sha256",
            "runtime_manifest_sha256",
            "selector_generation",
            "release_path",
            "launch_mode",
            "selector_state_path",
            "selector_lock_path",
            "launchd_label",
            "startup_fenced",
            "startup_transaction_id",
        )
    } == {
        "status": "managed",
        "valid": True,
        "build_id": "build-1",
        "commit": "a" * 40,
        "tree": "b" * 40,
        "manifest_sha256": release["manifest_sha256"],
        "agent_commit": "e" * 40,
        "agent_tree": "f" * 40,
        "agent_manifest_sha256": release["agent_source"]["manifest_sha256"],
        "runtime_manifest_sha256": release["runtime"]["identity"][
            "manifest_sha256"
        ],
        "selector_generation": 7,
        "release_path": str(release["release_path"].resolve()),
        "launch_mode": "selector",
        "selector_state_path": str(
            release["release_root"].parent / "selector.json"
        ),
        "selector_lock_path": str(
            release["release_root"].parent / "selector.lock"
        ),
        "launchd_label": "com.example.hermes-webui",
        "startup_fenced": False,
        "startup_transaction_id": None,
    }
    assert managed["attestation"] == "fresh"
    assert managed["verification_age_seconds"] >= 0

    startup_transaction = "candidate-startup-transaction-00001"
    monkeypatch.setenv("HERMES_WEBUI_STARTUP_FENCED", "1")
    monkeypatch.setenv(
        "HERMES_WEBUI_STARTUP_TRANSACTION_ID",
        startup_transaction,
    )
    candidate_identity = build_identity.get_build_identity(refresh=True)
    assert candidate_identity["startup_fenced"] is True
    assert candidate_identity["startup_transaction_id"] == startup_transaction
    monkeypatch.delenv("HERMES_WEBUI_STARTUP_FENCED")
    monkeypatch.delenv("HERMES_WEBUI_STARTUP_TRANSACTION_ID")

    wrong_interpreter = tmp_path / "runtime" / "wrong-python"
    _write(wrong_interpreter, "wrong interpreter\n")
    monkeypatch.setattr(
        build_identity, "_running_interpreter", lambda: wrong_interpreter.resolve()
    )
    wrong_runtime = build_identity.get_build_identity(refresh=True)
    assert wrong_runtime["status"] == "invalid"
    assert wrong_runtime["error_code"] == "manifest_verification_failed"
    monkeypatch.setattr(
        build_identity,
        "_running_interpreter",
        lambda: release["interpreter_path"].resolve(),
    )

    _chmod(release["release_path"] / "bootstrap.py", 0o644)
    _write(release["release_path"] / "bootstrap.py", "tampered\n")
    _chmod(release["release_path"] / "bootstrap.py", 0o444)
    invalid = build_identity.get_build_identity(refresh=True)
    assert invalid["status"] == "invalid"
    assert invalid["valid"] is False
    assert invalid["error_code"] == "manifest_verification_failed"


def test_direct_fallback_health_does_not_depend_on_external_selector(
    monkeypatch, tmp_path
):
    release = _managed_release(tmp_path)
    _attest_runtime_agent(monkeypatch, release)
    environment = _managed_env(release)
    environment["HERMES_WEBUI_LAUNCH_MODE"] = "direct-fallback"
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        build_identity, "_running_code_root", lambda: release["release_path"]
    )
    monkeypatch.setattr(
        build_identity,
        "_running_interpreter",
        lambda: release["interpreter_path"].absolute(),
    )

    release["selector_path"].unlink()

    identity = build_identity.get_build_identity(refresh=True)
    assert identity["status"] == "managed"
    assert identity["valid"] is True
    assert identity["launch_mode"] == "direct-fallback"


def test_managed_health_rejects_present_but_empty_environment(monkeypatch):
    for key in build_identity.MANAGED_ENV_KEYS:
        monkeypatch.setenv(key, "")

    identity = build_identity.get_build_identity(refresh=True)

    assert identity["status"] == "invalid"
    assert identity["valid"] is False
    assert identity["error_code"] == "empty_managed_environment"


def test_unmanaged_agent_dir_setting_does_not_claim_managed_release(monkeypatch, tmp_path):
    for key in build_identity.MANAGED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(tmp_path / "development-agent"))

    assert build_identity.get_build_identity(refresh=True) == {
        "status": "unmanaged",
        "valid": False,
    }


def test_unmanaged_build_identity_imports_without_posix_selector(monkeypatch):
    for key in build_identity.MANAGED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    original_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name == "scripts.webui_release_selector":
            raise AssertionError("unmanaged identity must not import the POSIX selector")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    assert build_identity.get_build_identity(refresh=True)["status"] == "unmanaged"


def test_managed_health_detects_post_start_release_drift(monkeypatch, tmp_path):
    release = _managed_release(tmp_path)
    _attest_runtime_agent(monkeypatch, release)
    for key, value in _managed_env(release).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        build_identity, "_running_code_root", lambda: release["release_path"]
    )
    monkeypatch.setattr(
        build_identity,
        "_running_interpreter",
        lambda: release["interpreter_path"].absolute(),
    )
    assert build_identity.get_build_identity(refresh=True)["valid"] is True
    bootstrap = release["release_path"] / "bootstrap.py"
    _chmod(bootstrap, 0o644)
    _write(bootstrap, "drifted after startup\n")
    _chmod(bootstrap, 0o444)

    refreshed = build_identity.get_build_identity(refresh=True)

    assert refreshed["status"] == "invalid"
    assert refreshed["error_code"] == "manifest_verification_failed"


@pytest.mark.parametrize("runtime_mismatch", ["configured-root", "imported-module"])
def test_managed_health_rejects_wrong_runtime_agent_identity(
    monkeypatch, tmp_path, runtime_mismatch
):
    release = _managed_release(tmp_path)
    outside = tmp_path / "outside-agent"
    _write(outside / "run_agent.py", "OUTSIDE = True\n")
    if runtime_mismatch == "configured-root":
        _attest_runtime_agent(monkeypatch, release, agent_dir=outside)
    else:
        _attest_runtime_agent(
            monkeypatch,
            release,
            run_agent_file=outside / "run_agent.py",
        )
    for key, value in _managed_env(release).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        build_identity, "_running_code_root", lambda: release["release_path"]
    )
    monkeypatch.setattr(
        build_identity,
        "_running_interpreter",
        lambda: release["interpreter_path"].absolute(),
    )

    identity = build_identity.get_build_identity(refresh=True)

    assert identity["status"] == "invalid"
    assert identity["error_code"] == "manifest_verification_failed"


@pytest.mark.parametrize(
    "module_name",
    ["agent", "hermes_cli", "tools", "tools.process_registry"],
)
def test_managed_health_attests_every_imported_agent_module_root(
    monkeypatch, tmp_path, module_name
):
    release = _managed_release(tmp_path)
    outside = tmp_path / "outside-agent" / Path(*module_name.split("."))
    outside = outside.with_suffix(".py")
    _write(outside, "OUTSIDE = True\n")
    _attest_runtime_agent(
        monkeypatch,
        release,
        module_files={module_name: outside},
    )
    for key, value in _managed_env(release).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        build_identity, "_running_code_root", lambda: release["release_path"]
    )
    monkeypatch.setattr(
        build_identity,
        "_running_interpreter",
        lambda: release["interpreter_path"].absolute(),
    )

    identity = build_identity.get_build_identity(refresh=True)

    assert identity["status"] == "invalid"
    assert identity["error_code"] == "manifest_verification_failed"


def test_managed_health_detects_post_start_agent_source_drift(monkeypatch, tmp_path):
    release = _managed_release(tmp_path)
    _attest_runtime_agent(monkeypatch, release)
    for key, value in _managed_env(release).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        build_identity, "_running_code_root", lambda: release["release_path"]
    )
    monkeypatch.setattr(
        build_identity,
        "_running_interpreter",
        lambda: release["interpreter_path"].absolute(),
    )
    assert build_identity.get_build_identity(refresh=True)["valid"] is True
    run_agent = release["agent_source"]["source_path"] / "run_agent.py"
    _chmod(run_agent, 0o644)
    _write(run_agent, "DRIFTED = True\n")
    _chmod(run_agent, 0o444)

    refreshed = build_identity.get_build_identity(refresh=True)

    assert refreshed["status"] == "invalid"
    assert refreshed["error_code"] == "manifest_verification_failed"


def test_plain_health_attestation_is_bounded_singleflight(monkeypatch):
    calls = []
    threads = []
    monkeypatch.setattr(
        build_identity,
        "_compute_identity",
        lambda: calls.append("compute") or {"status": "managed", "valid": True},
    )
    monkeypatch.setattr(build_identity, "_CACHED_ENV_SIGNATURE", None)
    monkeypatch.setattr(build_identity, "_CACHED_IDENTITY", None)
    monkeypatch.setattr(build_identity, "_CACHED_VERIFIED_AT", None)
    monkeypatch.setattr(build_identity, "_CACHED_MONOTONIC", None)
    monkeypatch.setattr(build_identity, "_REFRESH_IN_PROGRESS", False)
    build_identity.get_build_identity(refresh=True)
    monkeypatch.setattr(
        build_identity,
        "_CACHED_MONOTONIC",
        time.monotonic() - build_identity.ATTESTATION_TTL_SECONDS - 1,
    )

    class DeferredThread:
        def __init__(self, *, target, args, **_kwargs):
            threads.append((target, args))

        def start(self):
            return None

    monkeypatch.setattr(build_identity.threading, "Thread", DeferredThread)

    results = [build_identity.get_build_identity() for _ in range(25)]

    assert calls == ["compute"]
    assert len(threads) == 1
    assert all(result["attestation"] == "refreshing" for result in results)


def test_selector_state_lifecycle_and_immutable_bootstrap_fallback(tmp_path):
    base = _managed_release(tmp_path, "base")
    candidate = _managed_release(tmp_path, "candidate")
    candidate_2 = _managed_release(tmp_path, "candidate-2")
    state_path = tmp_path / "selector.json"
    lock_path = state_path.with_suffix(".lock")
    first_transaction = "selector-lifecycle-transaction-000001"
    second_transaction = "selector-lifecycle-transaction-000002"

    state = selector.initialize_selector_state(
        state_path,
        lock_path=lock_path,
        release_root=base["release_root"],
        bootstrap_build_id="base",
        bootstrap_record=base["record"],
    )
    assert state["generation"] == 0
    assert state["current"] == state["last_good"] == state["bootstrap_fallback"] == "base"

    state = selector.update_selector_state(
        state_path,
        lock_path=lock_path,
        expected_generation=0,
        transition=lambda current: selector.stage_candidate(
            current,
            "candidate",
            candidate["record"],
            transaction_id=first_transaction,
        ),
    )
    assert state["candidate"] == "candidate" and state["current"] == "base"
    state = selector.update_selector_state(
        state_path,
        lock_path=lock_path,
        expected_generation=1,
        transition=selector.activate_candidate,
    )
    assert state["current"] == "candidate" and state["last_good"] == "base"
    state = selector.update_selector_state(
        state_path,
        lock_path=lock_path,
        expected_generation=2,
        transition=selector.promote_candidate,
    )
    assert state["current"] == state["last_good"] == "candidate"
    assert state["candidate"] is None and state["bootstrap_fallback"] == "base"
    state = selector.update_selector_state(
        state_path,
        lock_path=lock_path,
        expected_generation=3,
        transition=lambda current: selector.stage_candidate(
            current,
            "candidate-2",
            candidate_2["record"],
            transaction_id=second_transaction,
        ),
    )
    assert state["generation"] == 4
    assert state["current"] == "candidate" and state["candidate"] == "candidate-2"
    state = selector.update_selector_state(
        state_path,
        lock_path=lock_path,
        expected_generation=4,
        transition=selector.activate_candidate,
    )
    assert state["generation"] == 5
    assert state["current"] == "candidate-2" and state["last_good"] == "candidate"
    state = selector.update_selector_state(
        state_path,
        lock_path=lock_path,
        expected_generation=5,
        transition=selector.rollback_to_last_good,
    )
    assert state["generation"] == 6
    assert state["current"] == "candidate"
    assert state["candidate"] is None
    assert state["bootstrap_fallback"] == "base"


def test_selector_v2_reads_v1_state_and_fences_candidate_with_exact_transaction(
    tmp_path,
):
    base = _managed_release(tmp_path, "base")
    candidate = _managed_release(tmp_path, "candidate")
    state_path = tmp_path / "selector.json"
    lock_path = state_path.with_suffix(".lock")
    state = selector.initialize_selector_state(
        state_path,
        lock_path=lock_path,
        release_root=base["release_root"],
        bootstrap_build_id="base",
        bootstrap_record=base["record"],
    )

    legacy = dict(state)
    legacy["version"] = 1
    legacy.pop("pending_transaction_id", None)
    state_path.write_text(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _chmod(state_path, 0o600)

    compatible = selector.read_selector_state(state_path, lock_path=lock_path)
    assert compatible["version"] == 2
    assert compatible["pending_transaction_id"] is None

    transaction_id = "transaction-candidate-000000000001"
    staged = selector.update_selector_state(
        state_path,
        lock_path=lock_path,
        expected_generation=0,
        transition=lambda current: selector.stage_candidate(
            current,
            "candidate",
            candidate["record"],
            transaction_id=transaction_id,
        ),
    )
    active = selector.update_selector_state(
        state_path,
        lock_path=lock_path,
        expected_generation=staged["generation"],
        transition=selector.activate_candidate,
    )
    selected = selector.resolve_selection(
        state_path,
        lock_path=lock_path,
        selector_path=base["selector_path"],
    )

    assert active["pending_transaction_id"] == transaction_id
    assert selected["environment"]["HERMES_WEBUI_STARTUP_FENCED"] == "1"
    assert (
        selected["environment"]["HERMES_WEBUI_STARTUP_TRANSACTION_ID"]
        == transaction_id
    )

    promoted = selector.promote_candidate(active)
    assert promoted["pending_transaction_id"] is None
    assert "HERMES_WEBUI_STARTUP_FENCED" not in selector._selection_from_state(
        promoted,
        selector_path=base["selector_path"],
    )["environment"]


def test_selector_refuses_to_activate_candidate_without_startup_transaction(tmp_path):
    base = _managed_release(tmp_path, "base")
    candidate = _managed_release(tmp_path, "candidate")
    state_path = tmp_path / "selector.json"
    lock_path = state_path.with_suffix(".lock")
    state = selector.initialize_selector_state(
        state_path,
        lock_path=lock_path,
        release_root=base["release_root"],
        bootstrap_build_id="base",
        bootstrap_record=base["record"],
    )
    staged = selector.stage_candidate(state, "candidate", candidate["record"])

    with pytest.raises(selector.SelectorError, match="transaction"):
        selector.activate_candidate(staged)


def test_selector_state_generation_and_path_guards(tmp_path):
    release = _managed_release(tmp_path)
    state_path = tmp_path / "selector.json"
    lock_path = state_path.with_suffix(".lock")
    selector.initialize_selector_state(
        state_path,
        lock_path=lock_path,
        release_root=release["release_root"],
        bootstrap_build_id=release["build_id"],
        bootstrap_record=release["record"],
    )

    with pytest.raises(selector.SelectorError, match="generation"):
        selector.update_selector_state(
            state_path,
            lock_path=lock_path,
            expected_generation=99,
            transition=selector.rollback_to_last_good,
        )
    with pytest.raises(selector.SelectorError, match="parent"):
        selector.read_selector_state(
            state_path,
            lock_path=tmp_path / "other" / "selector.lock",
        )

    with pytest.raises(selector.SelectorError, match="absolute"):
        selector.read_selector_state(
            Path("selector.json"),
            lock_path=Path("selector.lock"),
        )

    real_parent = tmp_path / "real-control"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-control"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(selector.SelectorError, match="symlinked ancestor"):
        selector.initialize_selector_state(
            linked_parent / "selector.json",
            lock_path=linked_parent / "selector.lock",
            release_root=release["release_root"],
            bootstrap_build_id=release["build_id"],
            bootstrap_record=release["record"],
        )

    lock_path.unlink()
    unrelated = tmp_path / "unrelated-lock-target"
    _write(unrelated, "must remain untouched\n")
    lock_path.symlink_to(unrelated)
    with pytest.raises(selector.SelectorError, match="must not be symlinks"):
        selector.read_selector_state(state_path, lock_path=lock_path)
    assert unrelated.read_text(encoding="utf-8") == "must remain untouched\n"


def test_selector_state_rejects_unsafe_mode_and_non_allowlisted_release_root(tmp_path):
    release = _managed_release(tmp_path)
    state_path = tmp_path / "selector.json"
    lock_path = state_path.with_suffix(".lock")
    selector.initialize_selector_state(
        state_path,
        lock_path=lock_path,
        release_root=release["release_root"],
        bootstrap_build_id=release["build_id"],
        bootstrap_record=release["record"],
    )
    _chmod(state_path, 0o666)
    with pytest.raises(selector.SelectorError, match="private mode"):
        selector.read_selector_state(state_path, lock_path=lock_path)

    other_root = tmp_path / "other-releases"
    other_root.mkdir()
    _chmod(other_root, 0o755)
    with pytest.raises(selector.SelectorError, match="allowlisted"):
        selector.initialize_selector_state(
            tmp_path / "other-selector.json",
            lock_path=tmp_path / "other-selector.lock",
            release_root=other_root,
            bootstrap_build_id=release["build_id"],
            bootstrap_record=release["record"],
        )


@pytest.mark.parametrize(
    ("crash_at", "activation_current", "rollback_current"),
    [
        ("after_temp_fsync", "base", "candidate"),
        ("after_replace", "candidate", "base"),
    ],
)
def test_selector_activation_and_rollback_crashes_leave_complete_old_or_new_state(
    tmp_path, crash_at, activation_current, rollback_current
):
    transaction_id = "selector-crash-transaction-000000001"
    base = _managed_release(tmp_path, "base")
    candidate = _managed_release(tmp_path, "candidate")
    state_path = tmp_path / "selector.json"
    lock_path = state_path.with_suffix(".lock")
    selector.initialize_selector_state(
        state_path,
        lock_path=lock_path,
        release_root=base["release_root"],
        bootstrap_build_id=base["build_id"],
        bootstrap_record=base["record"],
    )
    selector.update_selector_state(
        state_path,
        lock_path=lock_path,
        expected_generation=0,
        transition=lambda current: selector.stage_candidate(
            current,
            "candidate",
            candidate["record"],
            transaction_id=transaction_id,
        ),
    )

    with pytest.raises(selector.InjectedCrash):
        selector.update_selector_state(
            state_path,
            lock_path=lock_path,
            expected_generation=1,
            transition=selector.activate_candidate,
            crash_at=crash_at,
        )

    state = selector.read_selector_state(state_path, lock_path=lock_path)
    assert state["current"] == activation_current
    assert state["candidate"] == "candidate"
    assert state["last_good"] == state["bootstrap_fallback"] == "base"
    if state["current"] == "base":
        state = selector.update_selector_state(
            state_path,
            lock_path=lock_path,
            expected_generation=1,
            transition=selector.activate_candidate,
        )

    with pytest.raises(selector.InjectedCrash):
        selector.update_selector_state(
            state_path,
            lock_path=lock_path,
            expected_generation=2,
            transition=selector.rollback_to_last_good,
            crash_at=crash_at,
        )

    state = selector.read_selector_state(state_path, lock_path=lock_path)
    assert state["current"] == rollback_current
    assert state["candidate"] == ("candidate" if crash_at == "after_temp_fsync" else None)
    assert state["last_good"] == state["bootstrap_fallback"] == "base"


def test_resolve_selection_verifies_state_manifest_and_sets_identity_env(tmp_path):
    release = _managed_release(tmp_path)
    state_path = tmp_path / "selector.json"
    lock_path = state_path.with_suffix(".lock")
    selector.initialize_selector_state(
        state_path,
        lock_path=lock_path,
        release_root=release["release_root"],
        bootstrap_build_id=release["build_id"],
        bootstrap_record=release["record"],
    )

    selected = selector.resolve_selection(
        state_path,
        lock_path=lock_path,
        selector_path=release["selector_path"],
    )

    assert selected["bootstrap"] == release["release_path"] / "bootstrap.py"
    assert selected["interpreter"] == release["interpreter_path"].resolve()
    assert selected["environment"]["HERMES_WEBUI_SELECTOR_GENERATION"] == "0"
    assert selected["environment"]["HERMES_WEBUI_MANIFEST_SHA256"] == release[
        "manifest_sha256"
    ]
    assert selected["environment"]["PYTHONPATH"] == os.pathsep.join(
        [
            str(release["agent_source"]["source_path"]),
            release["runtime"]["identity"]["site_packages_path"],
        ]
    )
    assert selected["environment"]["HERMES_WEBUI_AUTO_INSTALL"] == "0"
    agent_identity = release["agent_source"]["identity"]
    assert {
        key: selected["environment"][key]
        for key in (
            "HERMES_WEBUI_AGENT_DIR",
            "HERMES_WEBUI_AGENT_COMMIT",
            "HERMES_WEBUI_AGENT_TREE",
            "HERMES_WEBUI_AGENT_MANIFEST_PATH",
            "HERMES_WEBUI_AGENT_MANIFEST_SHA256",
        )
    } == {
        "HERMES_WEBUI_AGENT_DIR": agent_identity["path"],
        "HERMES_WEBUI_AGENT_COMMIT": agent_identity["commit"],
        "HERMES_WEBUI_AGENT_TREE": agent_identity["tree"],
        "HERMES_WEBUI_AGENT_MANIFEST_PATH": agent_identity["manifest_path"],
        "HERMES_WEBUI_AGENT_MANIFEST_SHA256": agent_identity["manifest_sha256"],
    }


def test_resolve_selection_uses_exact_sealed_runtime_interpreter(tmp_path):
    release = _managed_release(tmp_path, symlink_interpreter=True)
    state_path = tmp_path / "selector.json"
    lock_path = state_path.with_suffix(".lock")
    selector.initialize_selector_state(
        state_path,
        lock_path=lock_path,
        release_root=release["release_root"],
        bootstrap_build_id=release["build_id"],
        bootstrap_record=release["record"],
    )

    selected = selector.resolve_selection(
        state_path,
        lock_path=lock_path,
        selector_path=release["selector_path"],
    )

    assert selected["interpreter"] == release["interpreter_path"]
    assert selected["interpreter"] == release["interpreter_path"].resolve()


def test_selector_main_execs_configured_venv_path_and_preserves_bootstrap_args(
    monkeypatch, tmp_path
):
    release = _managed_release(tmp_path, symlink_interpreter=True)
    captured = {}

    def fake_chdir(path):
        captured["chdir"] = Path(path)

    def fake_execve(executable, argv, environment):
        captured.update(
            executable=executable,
            argv=argv,
            environment=environment,
        )
        raise RuntimeError("execve intercepted")

    monkeypatch.setattr(
        selector,
        "_resolve_selection_unlocked",
        lambda *_args, **_kwargs: {
            "release_path": release["release_path"],
            "bootstrap": release["release_path"] / "bootstrap.py",
            "interpreter": release["interpreter_path"],
            "environment": {
                "HERMES_WEBUI_RELEASE_PATH": str(release["release_path"]),
                "HERMES_WEBUI_AGENT_DIR": str(release["agent_source"]["source_path"]),
                "PYTHONPATH": str(release["agent_source"]["source_path"]),
                "HERMES_WEBUI_AUTO_INSTALL": "0",
            },
        },
    )
    monkeypatch.setenv("PYTHONPATH", "/attacker/pythonpath")
    monkeypatch.setenv("PYTHONHOME", "/attacker/pythonhome")
    monkeypatch.setenv("PYTHONUSERBASE", "/attacker/userbase")
    monkeypatch.setenv("VIRTUAL_ENV", "/attacker/venv")
    monkeypatch.setenv("PYTHONINSPECT", "1")
    monkeypatch.setenv("PYTHONSTARTUP", "/attacker/startup.py")
    monkeypatch.setenv("HERMES_WEBUI_ATTACKER_PATH", "/attacker/hermes")
    monkeypatch.setenv("HERMES_WEBUI_AUTO_INSTALL", "1")
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", "/attacker/agent")
    monkeypatch.setattr(selector.os, "chdir", fake_chdir)
    monkeypatch.setattr(selector.os, "execve", fake_execve)

    with pytest.raises(RuntimeError, match="intercepted"):
        selector.main(
            [
                "--selector-state",
                str(tmp_path / "state.json"),
                "--selector-lock",
                str(tmp_path / "state.lock"),
                "--launchd-label",
                "com.example.hermes-webui",
                "--foreground",
                "--no-browser",
            ]
        )

    configured = str(release["interpreter_path"])
    assert captured["executable"] == configured
    assert captured["argv"] == [
        configured,
        "-S",
        str(release["release_path"] / "bootstrap.py"),
        "--foreground",
        "--no-browser",
    ]
    assert captured["chdir"] == release["release_path"]
    assert captured["environment"]["HERMES_WEBUI_RELEASE_PATH"] == str(
        release["release_path"]
    )
    assert captured["environment"]["PYTHONPATH"] == str(
        release["agent_source"]["source_path"]
    )
    assert captured["environment"]["HERMES_WEBUI_AGENT_DIR"] == str(
        release["agent_source"]["source_path"]
    )
    assert captured["environment"]["HERMES_WEBUI_AUTO_INSTALL"] == "0"
    assert captured["environment"]["HERMES_WEBUI_SELECTOR_STATE"] == str(
        tmp_path / "state.json"
    )
    assert captured["environment"]["HERMES_WEBUI_SELECTOR_LOCK"] == str(
        tmp_path / "state.lock"
    )
    assert captured["environment"]["HERMES_WEBUI_LAUNCHD_LABEL"] == (
        "com.example.hermes-webui"
    )
    for unsafe in (
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "PYTHONINSPECT",
        "PYTHONSTARTUP",
        "HERMES_WEBUI_ATTACKER_PATH",
    ):
        assert unsafe not in captured["environment"]
    assert captured["environment"]["PYTHONNOUSERSITE"] == "1"


def test_selector_main_holds_state_lock_until_exec_transition(monkeypatch, tmp_path):
    release = _managed_release(tmp_path)
    state_path = tmp_path / "selector.json"
    lock_path = state_path.with_suffix(".lock")
    selector.initialize_selector_state(
        state_path,
        lock_path=lock_path,
        release_root=release["release_root"],
        bootstrap_build_id=release["build_id"],
        bootstrap_record=release["record"],
    )
    attempted = threading.Event()
    completed = threading.Event()
    worker = {}

    def transition(state):
        return state

    def update_state():
        attempted.set()
        selector.update_selector_state(
            state_path,
            lock_path=lock_path,
            expected_generation=0,
            transition=transition,
        )
        completed.set()

    def fake_execve(_executable, _argv, _environment):
        thread = threading.Thread(target=update_state, daemon=True)
        worker["thread"] = thread
        thread.start()
        assert attempted.wait(1)
        assert not completed.wait(0.1)
        raise RuntimeError("execve intercepted")

    monkeypatch.setattr(selector.os, "chdir", lambda _path: None)
    monkeypatch.setattr(selector.os, "execve", fake_execve)
    monkeypatch.setattr(selector, "__file__", str(release["selector_path"]))

    with pytest.raises(RuntimeError, match="intercepted"):
        selector.main(
            [
                "--selector-state",
                str(state_path),
                "--selector-lock",
                str(lock_path),
                "--launchd-label",
                "com.example.hermes-webui",
                "--foreground",
            ]
        )

    worker["thread"].join(timeout=2)
    assert completed.is_set()
    assert selector.read_selector_state(state_path, lock_path=lock_path)[
        "generation"
    ] == 1


def test_launchd_transform_preserves_unrelated_fields_and_input(tmp_path):
    old_bootstrap = tmp_path / "old" / "bootstrap.py"
    interpreter = tmp_path / "venv" / "bin" / "python"
    selector_path = tmp_path / "control" / "selector.py"
    _write(old_bootstrap, "print('old')\n")
    _write(interpreter, "python\n")
    _write(selector_path, "# selector\n")
    selector_state = tmp_path / "control" / "selector.json"
    selector_lock = tmp_path / "control" / "selector.lock"
    _write(selector_state, "{}\n")
    _write(selector_lock, "")
    _chmod(interpreter, 0o755)
    _chmod(selector_path, 0o755)
    original = {
        "Label": "com.example.webui",
        "Program": "/attacker/python",
        "ProgramArguments": [
            str(interpreter),
            str(old_bootstrap),
            "--foreground",
        ],
        "WorkingDirectory": "/old",
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {
            "A": "B",
            "PYTHONPATH": "/attacker/pythonpath",
            "PYTHONHOME": "/attacker/pythonhome",
            "HERMES_WEBUI_AUTO_INSTALL": "1",
            "HERMES_WEBUI_AGENT_DIR": "/attacker/agent",
            "PYTHONUSERBASE": "/attacker/userbase",
            "VIRTUAL_ENV": "/attacker/venv",
            "PYTHONINSPECT": "1",
            "PYTHONSTARTUP": "/attacker/startup.py",
            "HERMES_WEBUI_ATTACKER_PATH": "/attacker/hermes",
            "HERMES_WEBUI_SELECTOR_STATE": "/wrong/state.json",
        },
        "StandardOutPath": "/logs/out",
    }
    before = copy.deepcopy(original)

    transformed = cutover.transform_launchd_target(
        original,
        str(selector_path),
        expected_label="com.example.webui",
        expected_old_interpreter=str(interpreter),
        managed_interpreter=str(interpreter),
        expected_old_target=str(old_bootstrap),
        selector_state_path=str(selector_state),
        selector_lock_path=str(selector_lock),
    )

    assert original == before
    assert transformed["ProgramArguments"] == [
        str(interpreter),
        "-S",
        str(selector_path),
        "--selector-state",
        str(selector_state),
        "--selector-lock",
        str(selector_lock),
        "--launchd-label",
        "com.example.webui",
        "--foreground",
    ]
    expected_unrelated = {
        key: value
        for key, value in transformed.items()
        if key not in {"ProgramArguments", "EnvironmentVariables", "WorkingDirectory"}
    }
    assert expected_unrelated == {
        key: value
        for key, value in original.items()
        if key not in {
            "Program",
            "ProgramArguments",
            "EnvironmentVariables",
            "WorkingDirectory",
        }
    }
    assert "Program" not in transformed
    assert transformed["WorkingDirectory"] == str(selector_state.parent)
    assert "A" not in transformed["EnvironmentVariables"]
    assert transformed["EnvironmentVariables"]["HERMES_WEBUI_SELECTOR_STATE"] == str(
        selector_state
    )
    assert transformed["EnvironmentVariables"]["HERMES_WEBUI_SELECTOR_LOCK"] == str(
        selector_lock
    )
    assert transformed["EnvironmentVariables"]["HERMES_WEBUI_LAUNCHD_LABEL"] == (
        "com.example.webui"
    )
    assert transformed["EnvironmentVariables"]["HERMES_WEBUI_AUTO_INSTALL"] == "0"
    for unsafe in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "PYTHONINSPECT",
        "PYTHONSTARTUP",
        "HERMES_WEBUI_AGENT_DIR",
        "HERMES_WEBUI_ATTACKER_PATH",
    ):
        assert unsafe not in transformed["EnvironmentVariables"]
    assert transformed["EnvironmentVariables"]["PYTHONNOUSERSITE"] == "1"


@pytest.mark.parametrize(
    "mutation",
    ["wrong-label", "python-module", "wrong-interpreter", "wrong-old-target"],
)
def test_launchd_transform_rejects_wrong_job_shape(tmp_path, mutation):
    interpreter = tmp_path / "venv" / "python"
    old_target = tmp_path / "old" / "bootstrap.py"
    selector_path = tmp_path / "control" / "selector.py"
    for path in (interpreter, old_target, selector_path):
        _write(path, "payload\n")
    selector_state = tmp_path / "control" / "selector.json"
    selector_lock = tmp_path / "control" / "selector.lock"
    _write(selector_state, "{}\n")
    _write(selector_lock, "")
    _chmod(interpreter, 0o755)
    _chmod(selector_path, 0o755)
    plist = {
        "Label": "com.example.webui",
        "ProgramArguments": [str(interpreter), str(old_target), "--foreground"],
    }
    if mutation == "wrong-label":
        plist["Label"] = "com.example.gateway"
    elif mutation == "python-module":
        plist["ProgramArguments"][1] = "-m"
    elif mutation == "wrong-interpreter":
        plist["ProgramArguments"][0] = str(tmp_path / "other-python")
    else:
        plist["ProgramArguments"][1] = str(tmp_path / "other-bootstrap.py")

    with pytest.raises(ValueError):
        cutover.transform_launchd_target(
            plist,
            str(selector_path),
            expected_label="com.example.webui",
            expected_old_interpreter=str(interpreter),
            managed_interpreter=str(interpreter),
            expected_old_target=str(old_target),
            selector_state_path=str(selector_state),
            selector_lock_path=str(selector_lock),
        )


def test_launchd_transform_rejects_inherited_selector_arguments(tmp_path):
    interpreter = tmp_path / "venv" / "python"
    old_target = tmp_path / "old" / "bootstrap.py"
    selector_path = tmp_path / "control" / "selector.py"
    selector_state = tmp_path / "control" / "selector.json"
    selector_lock = tmp_path / "control" / "selector.lock"
    for path in (interpreter, old_target, selector_path, selector_state, selector_lock):
        _write(path, "payload\n")
    _chmod(interpreter, 0o755)
    _chmod(selector_path, 0o755)
    plist = {
        "Label": "com.example.webui",
        "ProgramArguments": [
            str(interpreter),
            str(old_target),
            "--selector-state=/attacker/state",
        ],
    }

    with pytest.raises(ValueError, match="selector control argument"):
        cutover.transform_launchd_target(
            plist,
            str(selector_path),
            expected_label="com.example.webui",
            expected_old_interpreter=str(interpreter),
            managed_interpreter=str(interpreter),
            expected_old_target=str(old_target),
            selector_state_path=str(selector_state),
            selector_lock_path=str(selector_lock),
        )


def test_gateway_launchd_transform_binds_immutable_agent_runtime_and_routing(tmp_path):
    release = _managed_release(tmp_path)
    identity = {
        **selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        ),
        "selector_generation": 7,
    }
    transaction_id = "release-transaction-00000000000001"
    legacy_python = tmp_path / "legacy-agent" / "venv" / "bin" / "python"
    managed_shim = tmp_path / "control" / "hermes-candidate"
    _write(legacy_python, "#!/bin/sh\nexit 0\n")
    _write(managed_shim, "#!/bin/sh\nexit 0\n")
    _chmod(legacy_python, 0o755)
    _chmod(managed_shim, 0o555)
    routing = {
        "HERMES_WEBUI_DEFAULT_PROVIDER": "openai-codex",
        "HERMES_WEBUI_DEFAULT_MODEL": "gpt-5.5",
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_PORT": "8787",
    }
    original = {
        "Label": "ai.hermes.gateway",
        "Program": "/attacker/python",
        "ProgramArguments": [
            str(legacy_python),
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
            "--replace",
        ],
        "WorkingDirectory": str(tmp_path / "dirty-agent"),
        "KeepAlive": True,
        "EnvironmentVariables": {
            "PATH": "/attacker/bin",
            "PYTHONPATH": "/attacker/pythonpath",
            "PYTHONHOME": "/attacker/pythonhome",
            "VIRTUAL_ENV": "/attacker/venv",
            "HERMES_WEBUI_DEFAULT_PROVIDER": "wrong-provider",
            "HERMES_WEBUI_DEFAULT_MODEL": "wrong-model",
        },
    }

    transformed = cutover.transform_gateway_launchd_target(
        original,
        expected_label="ai.hermes.gateway",
        expected_old_program=str(legacy_python),
        managed_cli_shim=str(managed_shim),
        release_identity=identity,
        managed_routing_environment=routing,
        release_transaction_id=transaction_id,
    )

    assert transformed["ProgramArguments"] == [
        str(managed_shim),
        "gateway",
        "run",
        "--replace",
    ]
    assert transformed["WorkingDirectory"] == identity["agent_source_path"]
    assert transformed["KeepAlive"] is True
    assert "Program" not in transformed
    environment = transformed["EnvironmentVariables"]
    assert {key: environment[key] for key in routing} == routing
    assert environment["PYTHONHOME"] == identity["runtime_python_home_path"]
    assert environment["PYTHONPATH"] == os.pathsep.join(
        [identity["agent_source_path"], identity["runtime_site_packages_path"]]
    )
    assert environment["HERMES_WEBUI_AGENT_DIR"] == identity["agent_source_path"]
    assert environment["HERMES_WEBUI_LAUNCH_MODE"] == "managed-gateway"
    assert environment["HERMES_SELECTOR_GENERATION"] == "7"
    assert environment["HERMES_RELEASE_TRANSACTION_ID"] == transaction_id
    assert environment["HERMES_RELEASE_PAIR_ID"] == selector.release_pair_id(
        identity,
        selector_generation=7,
        transaction_id=transaction_id,
    )
    assert environment["PATH"].startswith(str(Path(identity["interpreter_path"]).parent))
    assert "VIRTUAL_ENV" not in environment


def test_gateway_launchd_transform_rebinds_exact_managed_gateway_shape(tmp_path):
    release = _managed_release(tmp_path)
    identity = {
        **selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        ),
        "selector_generation": 8,
    }
    transaction_id = "managed-release-transaction-0000000001"
    prior_shim = tmp_path / "control" / "hermes-prior"
    candidate_shim = tmp_path / "control" / "hermes-candidate"
    _write(prior_shim, "#!/bin/sh\nexit 0\n")
    _write(candidate_shim, "#!/bin/sh\nexit 0\n")
    _chmod(prior_shim, 0o555)
    _chmod(candidate_shim, 0o555)
    routing = {
        "HERMES_WEBUI_DEFAULT_PROVIDER": "openai-codex",
        "HERMES_WEBUI_DEFAULT_MODEL": "gpt-5.5",
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_PORT": "8787",
    }
    original = {
        "Label": "ai.hermes.gateway",
        "ProgramArguments": [
            str(prior_shim),
            "gateway",
            "run",
            "--replace",
        ],
        "WorkingDirectory": "/prior/immutable/agent",
        "EnvironmentVariables": {
            **routing,
            "HERMES_RELEASE_TRANSACTION_ID": (
                "prior-managed-transaction-000000001"
            ),
            "HERMES_RELEASE_PAIR_ID": "prior-pair",
            "PYTHONHOME": "/prior/runtime",
            "PYTHONPATH": "/prior/agent",
        },
    }

    transformed = cutover.transform_gateway_launchd_target(
        original,
        expected_label="ai.hermes.gateway",
        expected_old_program=str(prior_shim),
        managed_cli_shim=str(candidate_shim),
        release_identity=identity,
        managed_routing_environment=routing,
        release_transaction_id=transaction_id,
    )

    assert transformed["ProgramArguments"] == [
        str(candidate_shim),
        "gateway",
        "run",
        "--replace",
    ]
    assert transformed["WorkingDirectory"] == identity["agent_source_path"]
    assert transformed["EnvironmentVariables"][
        "HERMES_RELEASE_TRANSACTION_ID"
    ] == transaction_id
    assert transformed["EnvironmentVariables"][
        "HERMES_RELEASE_PAIR_ID"
    ] == selector.release_pair_id(
        identity,
        selector_generation=8,
        transaction_id=transaction_id,
    )


def test_gateway_launchd_transform_allows_attested_public_cli_symlink(tmp_path):
    release = _managed_release(tmp_path)
    identity = {
        **selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        ),
        "selector_generation": 8,
    }
    prior_shim = tmp_path / "control" / "hermes-prior"
    immutable_shim = tmp_path / "control" / "hermes-candidate"
    public_cli = tmp_path / "bin" / "hermes"
    _write(prior_shim, "#!/bin/sh\nexit 0\n")
    _write(immutable_shim, "#!/bin/sh\nexit 0\n")
    public_cli.parent.mkdir(exist_ok=True)
    public_cli.symlink_to(immutable_shim)
    _chmod(prior_shim, 0o555)
    _chmod(immutable_shim, 0o555)
    original = {
        "Label": "ai.hermes.gateway",
        "ProgramArguments": [
            str(prior_shim),
            "gateway",
            "run",
            "--replace",
        ],
    }
    routing = {
        "HERMES_WEBUI_DEFAULT_PROVIDER": "openai-codex",
        "HERMES_WEBUI_DEFAULT_MODEL": "gpt-5.5",
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_PORT": "8787",
    }

    with pytest.raises(ValueError, match="managed Hermes CLI shim must not be a symlink"):
        cutover.transform_gateway_launchd_target(
            original,
            expected_label="ai.hermes.gateway",
            expected_old_program=str(prior_shim),
            managed_cli_shim=str(public_cli),
            release_identity=identity,
            managed_routing_environment=routing,
            release_transaction_id="managed-release-transaction-0000000001",
        )

    transformed = cutover.transform_gateway_launchd_target(
        original,
        expected_label="ai.hermes.gateway",
        expected_old_program=str(prior_shim),
        managed_cli_shim=str(public_cli),
        release_identity=identity,
        managed_routing_environment=routing,
        release_transaction_id="managed-release-transaction-0000000001",
        allow_managed_cli_symlink=True,
    )

    assert transformed["ProgramArguments"][0] == str(public_cli)


@pytest.mark.parametrize(
    "managed_arguments",
    [
        ["gateway"],
        ["gateway", "status"],
        ["other", "run"],
    ],
)
def test_gateway_launchd_transform_rejects_ambiguous_managed_shape(
    tmp_path,
    managed_arguments,
):
    release = _managed_release(tmp_path)
    identity = {
        **selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        ),
        "selector_generation": 8,
    }
    prior_shim = tmp_path / "control" / "hermes-prior"
    candidate_shim = tmp_path / "control" / "hermes-candidate"
    _write(prior_shim, "#!/bin/sh\nexit 0\n")
    _write(candidate_shim, "#!/bin/sh\nexit 0\n")
    _chmod(prior_shim, 0o555)
    _chmod(candidate_shim, 0o555)

    with pytest.raises(ValueError, match="gateway launchd"):
        cutover.transform_gateway_launchd_target(
            {
                "Label": "ai.hermes.gateway",
                "ProgramArguments": [
                    str(prior_shim),
                    *managed_arguments,
                ],
            },
            expected_label="ai.hermes.gateway",
            expected_old_program=str(prior_shim),
            managed_cli_shim=str(candidate_shim),
            release_identity=identity,
            managed_routing_environment={
                "HERMES_WEBUI_DEFAULT_PROVIDER": "openai-codex",
                "HERMES_WEBUI_DEFAULT_MODEL": "gpt-5.5",
                "HERMES_WEBUI_HOST": "127.0.0.1",
                "HERMES_WEBUI_PORT": "8787",
            },
            release_transaction_id=(
                "managed-release-transaction-0000000001"
            ),
        )


@pytest.mark.parametrize("mutation", ["wrong-label", "wrong-module", "wrong-program"])
def test_gateway_launchd_transform_rejects_unattested_legacy_shape(
    tmp_path, mutation
):
    release = _managed_release(tmp_path)
    identity = {
        **selector.verify_release(
            release["release_path"],
            release_root=release["release_root"],
            expected_manifest_sha256=release["manifest_sha256"],
            selector_path=release["selector_path"],
        ),
        "selector_generation": 7,
    }
    legacy_python = tmp_path / "legacy" / "python"
    managed_shim = tmp_path / "managed" / "hermes"
    _write(legacy_python, "python\n")
    _write(managed_shim, "#!/bin/sh\n")
    _chmod(legacy_python, 0o755)
    _chmod(managed_shim, 0o555)
    plist = {
        "Label": "ai.hermes.gateway",
        "ProgramArguments": [
            str(legacy_python),
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
        ],
    }
    expected_program = str(legacy_python)
    if mutation == "wrong-label":
        plist["Label"] = "ai.hermes.other"
    elif mutation == "wrong-module":
        plist["ProgramArguments"][2] = "attacker.main"
    else:
        expected_program = str(tmp_path / "other-python")

    with pytest.raises(ValueError, match="gateway launchd"):
        cutover.transform_gateway_launchd_target(
            plist,
            expected_label="ai.hermes.gateway",
            expected_old_program=expected_program,
            managed_cli_shim=str(managed_shim),
            release_identity=identity,
            managed_routing_environment={
                "HERMES_WEBUI_DEFAULT_PROVIDER": "openai-codex",
                "HERMES_WEBUI_DEFAULT_MODEL": "gpt-5.5",
                "HERMES_WEBUI_HOST": "127.0.0.1",
                "HERMES_WEBUI_PORT": "8787",
            },
            release_transaction_id="release-transaction-00000000000001",
        )


def test_fallback_plist_reports_managed_last_good_identity(tmp_path):
    release = _managed_release(tmp_path)
    old_target = tmp_path / "old" / "bootstrap.py"
    _write(old_target, "print('old')\n")
    original = {
        "Label": "com.example.webui",
        "Program": "/attacker/python",
        "ProgramArguments": [
            str(release["interpreter_path"]),
            str(old_target),
            "--foreground",
            "--no-browser",
        ],
        "WorkingDirectory": str(tmp_path / "dirty-checkout"),
        "EnvironmentVariables": {
            "PRESERVE_ME": "yes",
            "PYTHONPATH": "/attacker/pythonpath",
            "PYTHONHOME": "/attacker/pythonhome",
            "HERMES_WEBUI_AUTO_INSTALL": "1",
            "HERMES_WEBUI_AGENT_DIR": "/attacker/agent",
            "PYTHONUSERBASE": "/attacker/userbase",
            "VIRTUAL_ENV": "/attacker/venv",
            "PYTHONINSPECT": "1",
            "PYTHONSTARTUP": "/attacker/startup.py",
            "HERMES_WEBUI_ATTACKER_PATH": "/attacker/hermes",
        },
    }
    identity = selector.verify_release(
        release["release_path"],
        release_root=release["release_root"],
        expected_manifest_sha256=release["manifest_sha256"],
        selector_path=release["selector_path"],
    )

    fallback = cutover.build_direct_fallback_plist(
        original,
        expected_label="com.example.webui",
        expected_old_interpreter=str(release["interpreter_path"]),
        expected_old_target=str(old_target),
        release_identity=identity,
        selector_generation=9,
        selector_state_path=str(tmp_path / "control" / "selector.json"),
        selector_lock_path=str(tmp_path / "control" / "selector.lock"),
    )

    assert fallback["ProgramArguments"] == [
        str(release["interpreter_path"]),
        "-S",
        str(release["release_path"] / "bootstrap.py"),
        "--foreground",
        "--no-browser",
    ]
    assert "Program" not in fallback
    assert fallback["WorkingDirectory"] == str(release["release_path"])
    environment = fallback["EnvironmentVariables"]
    assert "PRESERVE_ME" not in environment
    assert environment["HERMES_WEBUI_LAUNCH_MODE"] == "direct-fallback"
    assert environment["HERMES_WEBUI_RELEASE_PATH"] == str(release["release_path"])
    assert environment["HERMES_WEBUI_SERVER_CWD"] == str(release["release_path"])
    assert environment["HERMES_WEBUI_PYTHON"] == str(release["interpreter_path"])
    assert environment["HERMES_WEBUI_AUTO_INSTALL"] == "0"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONHOME"] == release["runtime"]["identity"][
        "python_home_path"
    ]
    assert environment["PYTHONPATH"] == os.pathsep.join(
        [
            str(release["agent_source"]["source_path"]),
            release["runtime"]["identity"]["site_packages_path"],
        ]
    )
    assert environment["HERMES_WEBUI_SELECTOR_STATE"] == str(
        tmp_path / "control" / "selector.json"
    )
    assert environment["HERMES_WEBUI_SELECTOR_LOCK"] == str(
        tmp_path / "control" / "selector.lock"
    )
    assert environment["HERMES_WEBUI_LAUNCHD_LABEL"] == "com.example.webui"
    for unsafe in (
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "PYTHONINSPECT",
        "PYTHONSTARTUP",
        "HERMES_WEBUI_ATTACKER_PATH",
    ):
        assert unsafe not in environment
    agent_identity = release["agent_source"]["identity"]
    assert environment["HERMES_WEBUI_AGENT_DIR"] == agent_identity["path"]
    assert environment["HERMES_WEBUI_AGENT_COMMIT"] == agent_identity["commit"]
    assert environment["HERMES_WEBUI_AGENT_TREE"] == agent_identity["tree"]
    assert environment["HERMES_WEBUI_AGENT_MANIFEST_PATH"] == agent_identity[
        "manifest_path"
    ]
    assert environment["HERMES_WEBUI_AGENT_MANIFEST_SHA256"] == agent_identity[
        "manifest_sha256"
    ]


def test_fallback_plist_generation_survives_deleted_selector(tmp_path):
    release = _managed_release(tmp_path)
    old_target = tmp_path / "old" / "bootstrap.py"
    _write(old_target, "print('old')\n")
    original = {
        "Label": "com.example.webui",
        "ProgramArguments": [
            str(release["interpreter_path"]),
            str(old_target),
            "--foreground",
        ],
    }
    identity = selector.verify_release(
        release["release_path"],
        release_root=release["release_root"],
        expected_manifest_sha256=release["manifest_sha256"],
        selector_path=release["selector_path"],
    )
    release["selector_path"].unlink()

    fallback = cutover.build_direct_fallback_plist(
        original,
        expected_label="com.example.webui",
        expected_old_interpreter=str(release["interpreter_path"]),
        expected_old_target=str(old_target),
        release_identity=identity,
        selector_generation=9,
        selector_state_path=str(tmp_path / "control" / "selector.json"),
        selector_lock_path=str(tmp_path / "control" / "selector.lock"),
    )

    assert fallback["ProgramArguments"][1] == "-S"
    assert fallback["ProgramArguments"][2] == str(
        release["release_path"] / "bootstrap.py"
    )
    assert fallback["EnvironmentVariables"]["HERMES_WEBUI_LAUNCH_MODE"] == (
        "direct-fallback"
    )


def test_fallback_plist_rejects_forged_release_identity(tmp_path):
    release = _managed_release(tmp_path)
    old_target = tmp_path / "old" / "bootstrap.py"
    _write(old_target, "print('old')\n")
    original = {
        "Label": "com.example.webui",
        "ProgramArguments": [
            str(release["interpreter_path"]),
            str(old_target),
            "--foreground",
        ],
    }
    identity = selector.verify_release(
        release["release_path"],
        release_root=release["release_root"],
        expected_manifest_sha256=release["manifest_sha256"],
        selector_path=release["selector_path"],
    )
    identity["manifest_sha256"] = "0" * 64

    with pytest.raises(selector.SelectorError, match="manifest hash"):
        cutover.build_direct_fallback_plist(
            original,
            expected_label="com.example.webui",
            expected_old_interpreter=str(release["interpreter_path"]),
            expected_old_target=str(old_target),
            release_identity=identity,
            selector_generation=9,
            selector_state_path=str(tmp_path / "control" / "selector.json"),
            selector_lock_path=str(tmp_path / "control" / "selector.lock"),
        )


def test_fallback_plist_rejects_drifted_agent_source(tmp_path):
    release = _managed_release(tmp_path)
    old_target = tmp_path / "old" / "bootstrap.py"
    _write(old_target, "print('old')\n")
    original = {
        "Label": "com.example.webui",
        "ProgramArguments": [
            str(release["interpreter_path"]),
            str(old_target),
            "--foreground",
        ],
    }
    identity = selector.verify_release(
        release["release_path"],
        release_root=release["release_root"],
        expected_manifest_sha256=release["manifest_sha256"],
        selector_path=release["selector_path"],
    )
    run_agent = release["agent_source"]["source_path"] / "run_agent.py"
    _chmod(run_agent, 0o644)
    _write(run_agent, "DRIFTED = True\n")
    _chmod(run_agent, 0o444)

    with pytest.raises(selector.SelectorError, match="agent source"):
        cutover.build_direct_fallback_plist(
            original,
            expected_label="com.example.webui",
            expected_old_interpreter=str(release["interpreter_path"]),
            expected_old_target=str(old_target),
            release_identity=identity,
            selector_generation=9,
            selector_state_path=str(tmp_path / "control" / "selector.json"),
            selector_lock_path=str(tmp_path / "control" / "selector.lock"),
        )


def test_cutover_cli_materializes_selector_and_fallback_plists(tmp_path, capsys):
    release = _managed_release(tmp_path)
    old_target = tmp_path / "old" / "bootstrap.py"
    _write(old_target, "print('old')\n")
    original = {
        "Label": "com.example.webui",
        "ProgramArguments": [
            str(release["interpreter_path"]),
            str(old_target),
            "--foreground",
        ],
        "WorkingDirectory": str(tmp_path / "dirty-checkout"),
        "EnvironmentVariables": {"PRESERVE_ME": "yes"},
    }
    source_plist = tmp_path / "source.plist"
    selector_plist = tmp_path / "selector.plist"
    fallback_plist = tmp_path / "fallback.plist"
    selector_state = tmp_path / "control" / "selector.json"
    selector_lock = tmp_path / "control" / "selector.lock"
    _write(selector_state, "{}\n")
    _write(selector_lock, "")
    source_plist.write_bytes(plistlib.dumps(original))
    identity = selector.verify_release(
        release["release_path"],
        release_root=release["release_root"],
        expected_manifest_sha256=release["manifest_sha256"],
        selector_path=release["selector_path"],
    )
    identity_json = tmp_path / "identity.json"
    identity_json.write_text(json.dumps(identity), encoding="utf-8")

    assert (
        cutover.main(
            [
                "plist-selector",
                "--input",
                str(source_plist),
                "--output",
                str(selector_plist),
                "--selector",
                str(release["selector_path"]),
                "--selector-state",
                str(selector_state),
                "--selector-lock",
                str(selector_lock),
                "--expected-label",
                "com.example.webui",
                "--expected-interpreter",
                str(release["interpreter_path"]),
                "--managed-interpreter",
                str(release["interpreter_path"]),
                "--expected-old-target",
                str(old_target),
            ]
        )
        == 0
    )
    selector_job = plistlib.loads(selector_plist.read_bytes())
    assert selector_job["ProgramArguments"][1:3] == [
        "-S",
        str(release["selector_path"]),
    ]
    assert selector_job["ProgramArguments"][3:7] == [
        "--selector-state",
        str(selector_state),
        "--selector-lock",
        str(selector_lock),
    ]
    assert "PRESERVE_ME" not in selector_job["EnvironmentVariables"]

    assert (
        cutover.main(
            [
                "plist-fallback",
                "--input",
                str(source_plist),
                "--output",
                str(fallback_plist),
                "--release-identity-json",
                str(identity_json),
                "--selector-generation",
                "9",
                "--expected-label",
                "com.example.webui",
                "--expected-interpreter",
                str(release["interpreter_path"]),
                "--expected-old-target",
                str(old_target),
                "--selector-state",
                str(selector_state),
                "--selector-lock",
                str(selector_lock),
            ]
        )
        == 0
    )
    fallback_job = plistlib.loads(fallback_plist.read_bytes())
    assert fallback_job["ProgramArguments"][1:3] == [
        "-S",
        str(release["release_path"] / "bootstrap.py"),
    ]
    assert fallback_job["EnvironmentVariables"]["HERMES_WEBUI_LAUNCH_MODE"] == (
        "direct-fallback"
    )
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "ok"


def test_cutover_cli_drives_selector_state_lifecycle(tmp_path, capsys):
    base = _managed_release(tmp_path, "base")
    candidate = _managed_release(tmp_path, "candidate")
    state_path = tmp_path / "selector.json"
    lock_path = tmp_path / "selector.lock"
    base_record = tmp_path / "base-record.json"
    candidate_record = tmp_path / "candidate-record.json"
    base_record.write_text(json.dumps(base["record"]), encoding="utf-8")
    candidate_record.write_text(json.dumps(candidate["record"]), encoding="utf-8")

    commands = [
        [
            "state-init",
            "--state",
            str(state_path),
            "--lock",
            str(lock_path),
            "--release-root",
            str(base["release_root"]),
            "--build-id",
            "base",
            "--record-json",
            str(base_record),
        ],
        [
            "state-stage",
            "--state",
            str(state_path),
            "--lock",
            str(lock_path),
            "--expected-generation",
            "0",
            "--build-id",
            "candidate",
            "--record-json",
            str(candidate_record),
            "--transaction-id",
            "selector-cli-transaction-00000001",
        ],
        [
            "state-activate",
            "--state",
            str(state_path),
            "--lock",
            str(lock_path),
            "--expected-generation",
            "1",
        ],
        [
            "state-promote",
            "--state",
            str(state_path),
            "--lock",
            str(lock_path),
            "--expected-generation",
            "2",
        ],
        [
            "state-rollback",
            "--state",
            str(state_path),
            "--lock",
            str(lock_path),
            "--expected-generation",
            "3",
        ],
        [
            "state-show",
            "--state",
            str(state_path),
            "--lock",
            str(lock_path),
        ],
    ]
    for command in commands:
        assert cutover.main(command) == 0

    final_state = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert final_state["generation"] == 4
    assert final_state["current"] == "candidate"
    assert final_state["last_good"] == "candidate"
    assert final_state["candidate"] is None


def test_transaction_journal_is_fsynced_idempotent_and_crash_resumable(tmp_path):
    journal_path = tmp_path / "control" / "transactions" / "txn.json"
    transaction_id = "journal-transaction-00000000000001"
    expected_candidate = {
        "build_id": "candidate",
        "manifest_sha256": "a" * 64,
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    initialized = cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=expected_candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "b" * 64,
            "state_snapshot_id": "snapshot-before-candidate",
            "state_snapshot_sha256": "c" * 64,
        },
    )
    assert initialized["phases"] == {}
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600

    cutover.record_transaction_phase(
        journal_path,
        transaction_id=transaction_id,
        phase="staged",
        receipt={"generation": 1, "build_id": "candidate"},
    )
    with pytest.raises(cutover.InjectedCutoverCrash, match="after_replace"):
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase="plist_installed",
            receipt={"plist_sha256": "c" * 64},
            crash_at="after_replace",
        )

    resumed = cutover.read_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
    )
    assert set(resumed["phases"]) == {"staged", "plist_installed"}
    assert cutover.record_transaction_phase(
        journal_path,
        transaction_id=transaction_id,
        phase="plist_installed",
        receipt={"plist_sha256": "c" * 64},
    ) == resumed
    with pytest.raises(cutover.ReleaseBuildError, match="different receipt"):
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase="plist_installed",
            receipt={"plist_sha256": "d" * 64},
        )
    with pytest.raises(cutover.ReleaseBuildError, match="sensitive"):
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase="old_fenced",
            receipt={"fence_token": "must-not-persist"},
        )


def _candidate_gateway_start_transaction(tmp_path: Path) -> tuple[dict, dict]:
    transaction_id = "paired-gateway-transaction-00000001"
    journal_path = tmp_path / "control" / "transaction.json"
    candidate = {
        "build_id": "candidate",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    last_good = {"build_id": "last-good"}
    runtime = {
        "pid": 41,
        "pid_start_token": "gateway-old-start",
        "command": "managed-last-good-gateway",
        "comm": "hermes",
        "cwd": "/immutable/agent-last-good",
        "program_arguments": ["/immutable/hermes-last-good", "gateway", "run"],
        "program_identity": {"sha256": "1" * 64},
    }
    binding = {
        "status": "verified",
        "listener_pid": 41,
        "launchd_pid": 41,
        "pid_start_token": "gateway-old-start",
        "runtime": runtime,
    }
    cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "a" * 64,
        "state_snapshot_id": "snapshot-before-candidate",
        "state_snapshot_sha256": "b" * 64,
        },
    )
    receipts = (
        ("staged", {}),
        ("plist_installed", {}),
        ("gateway_last_good_attested", {"binding": binding}),
        (
            "watchdog_cron_disable_intent",
            {"prepared": {"status": "prepared"}},
        ),
        (
            "watchdog_cron_disabled",
            {"status": "disabled"},
        ),
        (
            "watchdog_state_reconciled",
            {"status": "reconciled"},
        ),
        ("gateway_drain_intent", {"status": "drained"}),
        ("gateway_drained", {"status": "drained"}),
        ("gateway_stop_intent", {"status": "planned"}),
        ("gateway_gracefully_stopped", {"status": "stopped"}),
        ("gateway_dispatcher_lock_acquired", {"status": "locked"}),
        ("gateway_workers_quiescent", {"status": "quiescent"}),
        (
            "paired_state_snapshot_created",
            {
                "status": "created",
                "state_snapshot_id": transaction_id,
                "state_snapshot_sha256": "c" * 64,
            },
        ),
        ("gateway_dispatcher_lock_released", {"status": "released"}),
        (
            "candidate_gateway_start_intent",
            {
                "last_good_binding": binding,
                "candidate_build_id": candidate["build_id"],
                "candidate_shim_sha256": "d" * 64,
            },
        ),
    )
    for phase, receipt in receipts:
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipt,
        )
    plan = {
        "transaction_id": transaction_id,
        "transaction_journal": str(journal_path),
        "gateway_listener_port": 8642,
        "gateway_installed_plist": str(tmp_path / "gateway.plist"),
        "expected_candidate_identity": candidate,
        "last_good_identity": last_good,
    }
    return plan, binding


def test_candidate_gateway_resumes_after_joint_snapshot_before_candidate_start(
    tmp_path,
    monkeypatch,
):
    plan, _last_good_binding = _candidate_gateway_start_transaction(tmp_path)
    state = {"owner": "absent"}
    calls = {"start": 0}
    candidate_binding = {
        "status": "verified",
        "listener_pid": 42,
        "launchd_pid": 42,
        "pid_start_token": "gateway-candidate-start",
        "runtime": {"command": "candidate"},
    }

    def attest(_plan, identity, *, expected_admission=None):
        assert expected_admission == "rejecting_new_work"
        if state["owner"] == identity["build_id"]:
            return candidate_binding
        raise cutover.DrainIdentityMismatch("not current")

    def listener(_port):
        if state["owner"] == "absent":
            raise cutover.DrainIdentityMismatch("absent")
        return 42

    def start(*_args, **_kwargs):
        calls["start"] += 1
        state["owner"] = "candidate"
        return {"status": "started"}

    monkeypatch.setattr(cutover, "_attest_managed_gateway_binding", attest)
    monkeypatch.setattr(cutover, "_listener_pid", listener)
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, gateway: None if state["owner"] == "absent" else listener(8642),
    )
    monkeypatch.setattr(
        cutover,
        "_install_managed_gateway_plist",
        lambda *_args: {"status": "installed"},
    )
    monkeypatch.setattr(cutover, "_bootstrap_job", start)

    result = cutover._complete_candidate_gateway_transition(
        plan,
        {"status": "startup-fenced"},
    )

    assert result["gateway"]["binding"] == candidate_binding
    assert calls == {"start": 1}


def test_candidate_gateway_adopts_launch_after_crash_before_accept_receipt(
    tmp_path, monkeypatch
):
    plan, _last_good_binding = _candidate_gateway_start_transaction(tmp_path)
    state = {"owner": "absent"}
    calls = {"start": 0}
    candidate_binding = {
        "status": "verified",
        "listener_pid": 52,
        "launchd_pid": 52,
        "pid_start_token": "gateway-candidate-start",
        "runtime": {"command": "candidate"},
    }

    def attest(_plan, identity, *, expected_admission=None):
        assert expected_admission == "rejecting_new_work"
        if state["owner"] == identity["build_id"]:
            return candidate_binding
        raise cutover.DrainIdentityMismatch("not current")

    def listener(_port):
        if state["owner"] == "absent":
            raise cutover.DrainIdentityMismatch("absent")
        return 52

    def start(*_args, **_kwargs):
        calls["start"] += 1
        state["owner"] = "candidate"
        return {"status": "started"}

    monkeypatch.setattr(cutover, "_attest_managed_gateway_binding", attest)
    monkeypatch.setattr(cutover, "_listener_pid", listener)
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, gateway: None if state["owner"] == "absent" else listener(8642),
    )
    monkeypatch.setattr(
        cutover,
        "_install_managed_gateway_plist",
        lambda *_args: {"status": "installed"},
    )
    monkeypatch.setattr(cutover, "_bootstrap_job", start)
    real_record = cutover.record_transaction_phase
    crash_once = {"value": True}

    def crash_before_accept(*args, **kwargs):
        if kwargs.get("phase") == "candidate_gateway_accepted" and crash_once["value"]:
            crash_once["value"] = False
            raise cutover.InjectedCutoverCrash("before-gateway-accept-receipt")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(cutover, "record_transaction_phase", crash_before_accept)
    with pytest.raises(
        cutover.InjectedCutoverCrash, match="before-gateway-accept-receipt"
    ):
        cutover._complete_candidate_gateway_transition(
            plan,
            {"status": "startup-fenced"},
        )
    assert state["owner"] == "candidate"

    result = cutover._complete_candidate_gateway_transition(
        plan,
        {"status": "startup-fenced"},
    )

    assert result["gateway"]["binding"] == candidate_binding
    assert calls == {"start": 1}


def _watchdog_barrier_transaction(tmp_path: Path) -> tuple[dict, dict]:
    transaction_id = "watchdog-barrier-transaction-000001"
    journal_path = tmp_path / "watchdog-barrier.json"
    candidate = {
        "build_id": "candidate",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "a" * 64,
            "state_snapshot_id": "snapshot-before-candidate",
            "state_snapshot_sha256": "b" * 64,
        },
    )
    for phase, receipt in (
        ("staged", {}),
        ("plist_installed", {"plist_sha256": "c" * 64}),
        ("gateway_last_good_attested", {"binding": {"status": "verified"}}),
    ):
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipt,
        )
    return {
        "transaction_id": transaction_id,
        "transaction_journal": str(journal_path),
        "watchdog_crontab_rollback": str(tmp_path / "crontab.backup"),
        "watchdog_candidate_script": str(tmp_path / "candidate-watchdog.py"),
    }, candidate


def _internal_watchdog_plan(tmp_path: Path) -> tuple[dict, Path]:
    registry = tmp_path / "cron" / "jobs.json"
    registry.parent.mkdir(parents=True)
    registry.parent.chmod(0o700)
    watchdog = {
        "id": "watchdog-job",
        "name": "Hermes session stuck watchdog",
        "script": "hermes-session-watchdog.py",
        "enabled": True,
        "state": "scheduled",
        "deliver": "local",
        "no_agent": True,
        "repeat": {"completed": 5, "times": None},
        "next_run_at": "2026-07-23T12:00:00+00:00",
    }
    registry.write_text(
        json.dumps(
            {
                "jobs": [
                    watchdog,
                    {
                        "id": "unrelated-job",
                        "script": "unrelated.py",
                        "enabled": True,
                    },
                ],
                "updated_at": "2026-07-23T11:59:00+00:00",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    registry.chmod(0o600)
    return {
        "transaction_id": "internal-watchdog-transaction-000001",
        "watchdog_scheduler_backend": "hermes_internal",
        "watchdog_scheduler_registry": str(registry),
        "watchdog_scheduler_job_id": watchdog["id"],
        "watchdog_installed_script": str(
            tmp_path / "scripts" / "hermes-session-watchdog.py"
        ),
        "watchdog_crontab_rollback": str(tmp_path / "watchdog-job.backup.json"),
    }, registry


def test_internal_watchdog_registry_is_an_admitted_cutover_plan_path():
    assert "watchdog_scheduler_registry" in cutover._CUTOVER_PLAN_OPTIONAL
    assert "watchdog_scheduler_registry" in cutover._CUTOVER_PLAN_PATH_KEYS


def test_last_good_gateway_identity_is_an_admitted_cutover_plan_path():
    assert "last_good_gateway_identity_json" in cutover._CUTOVER_PLAN_OPTIONAL
    assert "last_good_gateway_identity_json" in cutover._CUTOVER_PLAN_PATH_KEYS


def test_expected_old_interpreter_plan_path_allows_only_its_leaf_symlink(tmp_path):
    interpreter = tmp_path / "python3.11"
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o700)
    configured = tmp_path / "python"
    configured.symlink_to(interpreter)

    assert cutover._absolute_plan_path(
        configured,
        label="expected_old_interpreter",
    ) == configured
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="installed_plist must not be a symlink",
    ):
        cutover._absolute_plan_path(configured, label="installed_plist")


def test_cutover_mutable_references_are_not_classified_as_artifacts():
    assert cutover._CUTOVER_MUTABLE_REFERENCE_PATH_KEYS == {
        "watchdog_state_file",
        "watchdog_scheduler_registry",
        "legacy_state_db",
        "synthetic_process_notifications_path",
        "synthetic_async_delegations_path",
    }
    assert not {
        "signing_key_file",
        "selector_state",
        "transaction_journal",
        "installed_plist",
    } & cutover._CUTOVER_MUTABLE_REFERENCE_PATH_KEYS


def test_internal_watchdog_receipt_is_scoped_to_exact_job(tmp_path):
    plan, registry = _internal_watchdog_plan(tmp_path)

    prepared = cutover._backup_crontab(plan)
    before = cutover._cron_watchdog_receipt(plan)

    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["jobs"][1]["last_run_at"] = "2026-07-23T12:00:05+00:00"
    payload["updated_at"] = "2026-07-23T12:00:05+00:00"
    registry.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    registry.chmod(0o600)

    assert cutover._cron_watchdog_receipt(plan) == before
    assert prepared["backend"] == "hermes_internal"
    assert prepared["job_id"] == "watchdog-job"
    assert prepared["backup_sha256"] == prepared["crontab_sha256"]
    assert json.loads(
        Path(prepared["backup_path"]).read_text(encoding="utf-8")
    )["id"] == "watchdog-job"


def test_internal_watchdog_os_duplicate_check_uses_exact_shell_tokens(
    tmp_path,
    monkeypatch,
):
    plan, _registry = _internal_watchdog_plan(tmp_path)
    installed = plan["watchdog_installed_script"]
    monkeypatch.setattr(
        cutover,
        "_read_crontab",
        lambda: (
            "0 * * * * echo hermes-session-watchdog.py.backup\n"
            "# 0 * * * * "
            + installed
            + "\n"
        ),
    )

    cutover._assert_no_os_watchdog_duplicate(plan)

    monkeypatch.setattr(
        cutover,
        "_read_crontab",
        lambda: f"*/3 * * * * /usr/bin/python3 {installed}\n",
    )
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="duplicate OS cron",
    ):
        cutover._assert_no_os_watchdog_duplicate(plan)


def test_prepare_release_watchdog_barrier_captures_internal_gateway_intent(
    tmp_path,
    monkeypatch,
):
    plan, _registry = _internal_watchdog_plan(tmp_path)
    cron = {
        "backend": "hermes_internal",
        "backup_path": str(tmp_path / "watchdog.backup"),
        "backup_sha256": "a" * 64,
        "crontab_sha256": "b" * 64,
        "watchdog_command": "hermes-internal:watchdog-job:watchdog.py",
    }
    gateway = {"pid": 123, "pid_start_token": "start-token"}
    intent = {"status": "prepared", "marker": {"sha256": "c" * 64}}
    monkeypatch.setattr(cutover, "_backup_crontab", lambda actual: cron.copy())
    monkeypatch.setattr(
        cutover,
        "_listener_process_receipt",
        lambda actual, *, gateway, require_git_source: {
            **gateway_receipt
        },
    )
    gateway_receipt = gateway
    monkeypatch.setattr(
        cutover,
        "_legacy_gateway_drain_intent_receipt",
        lambda actual_plan, prepared: intent
        if prepared["gateway"] == gateway
        else pytest.fail("gateway receipt was not captured"),
    )

    prepared = cutover._prepare_release_watchdog_barrier(plan)

    assert prepared == {
        "gateway": gateway,
        "watchdog_cron": {**cron, "drain_intent": intent},
    }


def test_internal_watchdog_barrier_pauses_registry_without_gateway_drain(
    tmp_path,
    monkeypatch,
):
    plan, registry = _internal_watchdog_plan(tmp_path)
    os_cron_reads = []
    monkeypatch.setattr(
        cutover,
        "_read_crontab",
        lambda: os_cron_reads.append(True) or "",
    )
    prepared = {
        **cutover._backup_crontab(plan),
        "drain_intent": {
            "marker": {
                "path": str(tmp_path / ".drain_request.json"),
                "payload": {"release_transaction_id": plan["transaction_id"]},
                "sha256": "a" * 64,
            }
        },
    }
    monkeypatch.setattr(
        cutover,
        "_wait_for_legacy_gateway_drain",
        lambda *_args: pytest.fail("legacy gateway drain must not be required"),
    )
    monkeypatch.setattr(
        cutover,
        "_attest_internal_watchdog_drain_marker",
        lambda *_args: pytest.fail("legacy gateway marker must not be required"),
    )
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["jobs"][0]["last_run_at"] = "2026-07-23T12:00:05+00:00"
    payload["jobs"][0]["next_run_at"] = "2026-07-23T12:03:05+00:00"
    payload["jobs"][0]["repeat"] = {"completed": 7, "times": None}
    unrelated_before = copy.deepcopy(payload["jobs"][1])
    registry.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    registry.chmod(0o600)

    disabled = cutover._disable_watchdog_cron(
        plan,
        {"watchdog_cron": prepared, "gateway": {"pid": 99}},
    )

    assert disabled["status"] == "disabled"
    assert disabled["backend"] == "hermes_internal"
    assert disabled["job_id"] == "watchdog-job"
    assert re.fullmatch(r"[0-9a-f]{64}", disabled["crontab_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", disabled["marker_sha256"])
    paused_payload = json.loads(registry.read_text(encoding="utf-8"))
    paused = paused_payload["jobs"][0]
    assert paused["enabled"] is False
    assert paused["state"] == "paused"
    assert paused["paused_reason"] == (
        "release-cutover:internal-watchdog-transaction-000001"
    )
    assert paused["last_run_at"] == "2026-07-23T12:00:05+00:00"
    assert paused["next_run_at"] == "2026-07-23T12:03:05+00:00"
    assert paused["repeat"] == {"completed": 7, "times": None}
    assert paused_payload["jobs"][1] == unrelated_before
    assert cutover._disable_watchdog_cron(
        plan,
        {"watchdog_cron": prepared, "gateway": {"pid": 99}},
    ) == disabled
    assert cutover._attest_disabled_watchdog_cron(
        plan,
        {"watchdog_cron": prepared},
    ) == disabled

    restored = cutover._restore_watchdog_cron(
        plan,
        {"watchdog_cron": prepared},
    )

    active_payload = json.loads(registry.read_text(encoding="utf-8"))
    active = active_payload["jobs"][0]
    assert active["enabled"] is True
    assert active["state"] == "scheduled"
    assert active.get("paused_at") is None
    assert active.get("paused_reason") is None
    assert active["last_run_at"] == "2026-07-23T12:00:05+00:00"
    assert active["next_run_at"] == "2026-07-23T12:03:05+00:00"
    assert active["repeat"] == {"completed": 7, "times": None}
    assert active_payload["jobs"][1] == unrelated_before
    assert restored["backend"] == "hermes_internal"
    assert cutover._restore_watchdog_cron(
        plan,
        {"watchdog_cron": prepared},
    )["stable_job_sha256"] == restored["stable_job_sha256"]
    assert os_cron_reads


def test_internal_watchdog_legacy_prepared_receipt_is_upgraded_from_backup(
    tmp_path,
):
    plan, registry = _internal_watchdog_plan(tmp_path)
    watchdog_cron = cutover._backup_crontab(plan)
    expected_stable = watchdog_cron.pop("stable_job_sha256")
    watchdog_cron["drain_intent"] = {
        "marker": {
            "path": str(tmp_path / ".drain_request.json"),
            "payload": {"release_transaction_id": plan["transaction_id"]},
            "sha256": "d" * 64,
        }
    }
    legacy = {
        "watchdog_cron": watchdog_cron,
        "gateway": {"pid": 99},
    }

    upgraded = cutover._upgrade_internal_watchdog_prepared_receipt(
        plan,
        legacy,
    )

    assert "stable_job_sha256" not in legacy["watchdog_cron"]
    assert upgraded["watchdog_cron"]["stable_job_sha256"] == expected_stable
    disabled = cutover._disable_watchdog_cron(plan, upgraded)
    assert disabled["stable_job_sha256"] == expected_stable
    restored = cutover._restore_watchdog_cron(plan, upgraded)
    assert restored["stable_job_sha256"] == expected_stable
    assert json.loads(registry.read_text(encoding="utf-8"))["jobs"][0][
        "enabled"
    ] is True


def test_internal_watchdog_registry_symlink_is_rejected(tmp_path):
    plan, registry = _internal_watchdog_plan(tmp_path)
    foreign = tmp_path / "foreign-jobs.json"
    foreign.write_bytes(registry.read_bytes())
    foreign.chmod(0o600)
    registry.unlink()
    registry.symlink_to(foreign)

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="internal watchdog registry is unreadable",
    ):
        cutover._read_internal_watchdog_registry(plan)


def test_internal_watchdog_restore_rejects_symlinked_backup(tmp_path):
    plan, _registry = _internal_watchdog_plan(tmp_path)
    watchdog_cron = cutover._backup_crontab(plan)
    watchdog_cron["drain_intent"] = {
        "marker": {
            "path": str(tmp_path / ".drain_request.json"),
            "payload": {"release_transaction_id": plan["transaction_id"]},
            "sha256": "e" * 64,
        }
    }
    prepared = {
        "watchdog_cron": watchdog_cron,
        "gateway": {"pid": 99},
    }
    cutover._disable_watchdog_cron(plan, prepared)
    backup = Path(plan["watchdog_crontab_rollback"])
    foreign = tmp_path / "foreign-watchdog-backup.json"
    foreign.write_bytes(backup.read_bytes())
    foreign.chmod(0o600)
    backup.unlink()
    backup.symlink_to(foreign)

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="internal watchdog rollback job is unreadable",
    ):
        cutover._restore_watchdog_cron(plan, prepared)


def test_internal_watchdog_attestation_rejects_job_identity_change(
    tmp_path,
    monkeypatch,
):
    plan, registry = _internal_watchdog_plan(tmp_path)
    prepared = {
        **cutover._backup_crontab(plan),
        "drain_intent": {
            "marker": {
                "path": str(tmp_path / ".drain_request.json"),
                "payload": {"release_transaction_id": plan["transaction_id"]},
                "sha256": "b" * 64,
            }
        },
    }
    monkeypatch.setattr(
        cutover,
        "_attest_internal_watchdog_drain_marker",
        lambda *_args: {"status": "verified", "marker_sha256": "b" * 64},
    )
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["jobs"][0]["enabled"] = False
    registry.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    registry.chmod(0o600)

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="internal watchdog job changed",
    ):
        cutover._attest_disabled_watchdog_cron(
            plan,
            {"watchdog_cron": prepared},
        )


def test_internal_watchdog_active_claim_refuses_pause_without_mutation(
    tmp_path,
    monkeypatch,
):
    plan, registry = _internal_watchdog_plan(tmp_path)
    intent = {
        "marker": {
            "path": str(tmp_path / ".drain_request.json"),
            "payload": {"release_transaction_id": plan["transaction_id"]},
            "sha256": "c" * 64,
        }
    }
    prepared = {
        "watchdog_cron": {
            **cutover._backup_crontab(plan),
            "drain_intent": intent,
        },
        "gateway": {"pid": 99},
    }
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["jobs"][0]["state"] = "running"
    payload["jobs"][0]["run_claim"] = {
        "owner": "legacy-scheduler",
        "claimed_at": "2026-07-23T12:00:00+00:00",
    }
    registry.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    registry.chmod(0o600)
    before = registry.read_bytes()
    monkeypatch.setattr(
        cutover,
        "_wait_for_legacy_gateway_drain",
        lambda *_args: pytest.fail("legacy gateway drain must not be required"),
    )
    monkeypatch.setattr(
        cutover,
        "_clear_legacy_gateway_drain_marker",
        lambda *_args: pytest.fail("no gateway marker should be written"),
    )

    with pytest.raises(cutover.DrainTimeout, match="watchdog job is active"):
        cutover._disable_watchdog_cron(plan, prepared)

    assert registry.read_bytes() == before


def test_begin_release_watchdog_barrier_prepares_internal_backend(
    tmp_path,
    monkeypatch,
):
    plan, _candidate = _watchdog_barrier_transaction(tmp_path)
    plan["watchdog_scheduler_backend"] = "hermes_internal"
    prepared = {
        "gateway": {"pid": 99, "pid_start_token": "start"},
        "watchdog_cron": {
            "backend": "hermes_internal",
            "backup_path": plan["watchdog_crontab_rollback"],
            "backup_sha256": "d" * 64,
            "crontab_sha256": "e" * 64,
            "watchdog_command": (
                "hermes-internal:watchdog-job:hermes-session-watchdog.py"
            ),
            "drain_intent": {"marker": {"sha256": "a" * 64}},
        },
    }
    expected_state = {
        "path": str(tmp_path / "state.json"),
        "exists": True,
        "sha256": "f" * 64,
        "schema_version": 1,
        "claim_revision": 7,
    }
    lock = object()
    prepared_calls = []
    monkeypatch.setattr(
        cutover,
        "_prepare_release_watchdog_barrier",
        lambda actual: prepared_calls.append(actual) or prepared,
    )
    monkeypatch.setattr(
        cutover,
        "_disable_watchdog_cron",
        lambda *_args: {"status": "disabled"},
    )
    monkeypatch.setattr(
        cutover,
        "_watchdog_reconcile_receipt",
        lambda *_args, **_kwargs: {
            "status": "no_reconcilable_slot",
            "state_before": expected_state,
            "state_after": expected_state,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_acquire_watchdog_state_lock",
        lambda _plan: lock,
    )
    monkeypatch.setattr(
        cutover,
        "_verify_watchdog_state_lock",
        lambda _plan, actual: {"status": "locked"}
        if actual is lock
        else pytest.fail("wrong lock"),
    )
    monkeypatch.setattr(
        cutover,
        "_watchdog_state_receipt",
        lambda _plan: expected_state,
    )

    barrier = cutover._begin_release_watchdog_barrier(plan)

    assert prepared_calls == [plan]
    assert barrier["prepared"] == prepared
    assert barrier["lock"] is lock


def test_watchdog_reconcile_retries_only_transient_empty_receipt_and_uses_candidate(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / "candidate-watchdog.py"
    candidate.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    candidate.chmod(0o700)
    plan = {
        "watchdog_state_file": str(tmp_path / "recovery" / "state.json"),
        "watchdog_installed_script": str(tmp_path / "installed-watchdog.py"),
        "watchdog_candidate_script": str(candidate),
        "watchdog_expected_sha256": "a" * 64,
        "signing_key_file": str(tmp_path / "webui" / ".signing_key"),
        "base_url": "http://127.0.0.1:8787",
        "listener_port": 8787,
        "timeout_seconds": 300,
        "interval_seconds": 0.25,
    }
    state_before = {"claim_revision": 7}
    state_after = {"claim_revision": 8}
    state_receipts = iter((state_before, state_after))
    invocations = []
    results = iter(
        (
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=(
                    "RECOVERY_SLOT_RECONCILE_ONLY "
                    "status=no_reconcilable_slot\n"
                ),
                stderr="",
            ),
        )
    )
    monotonic = iter((100.0, 100.1))
    sleeps = []
    monkeypatch.setattr(
        cutover,
        "_watchdog_state_receipt",
        lambda _plan: next(state_receipts),
    )
    monkeypatch.setattr(
        cutover,
        "_file_identity_receipt",
        lambda path: {
            "resolved_path": str(Path(path).resolve()),
            "sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_attest_disabled_watchdog_cron",
        lambda _plan, prepared: {"prepared": prepared},
    )
    monkeypatch.setattr(
        cutover.subprocess,
        "run",
        lambda argv, **_kwargs: invocations.append(argv) or next(results),
    )
    monkeypatch.setattr(cutover.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(
        cutover.time,
        "sleep",
        lambda seconds: sleeps.append(seconds),
    )

    receipt = cutover._watchdog_reconcile_receipt(
        plan,
        {"status": "prepared"},
        script_path=candidate,
    )

    assert invocations == [[str(candidate)], [str(candidate)]]
    assert sleeps == [0.25]
    assert receipt["status"] == "no_reconcilable_slot"
    assert receipt["transient_empty_attempts"] == 1
    assert receipt["installed_script"]["sha256"] == "a" * 64
    assert receipt["state_before"] == state_before
    assert receipt["state_after"] == state_after


def test_watchdog_reconcile_rejects_stderr_without_retry(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate-watchdog.py"
    candidate.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    candidate.chmod(0o700)
    plan = {
        "watchdog_state_file": str(tmp_path / "recovery" / "state.json"),
        "watchdog_installed_script": str(tmp_path / "installed-watchdog.py"),
        "watchdog_candidate_script": str(candidate),
        "watchdog_expected_sha256": "b" * 64,
        "signing_key_file": str(tmp_path / "webui" / ".signing_key"),
        "base_url": "http://127.0.0.1:8787",
        "listener_port": 8787,
        "timeout_seconds": 300,
        "interval_seconds": 0.25,
    }
    invocations = []
    monkeypatch.setattr(
        cutover,
        "_watchdog_state_receipt",
        lambda _plan: {"claim_revision": 7},
    )
    monkeypatch.setattr(
        cutover,
        "_file_identity_receipt",
        lambda path: {
            "resolved_path": str(Path(path).resolve()),
            "sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(
        cutover.subprocess,
        "run",
        lambda argv, **_kwargs: invocations.append(argv)
        or SimpleNamespace(returncode=0, stdout="", stderr="warning\n"),
    )

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="watchdog reconcile-only receipt is invalid",
    ):
        cutover._watchdog_reconcile_receipt(
            plan,
            {"status": "prepared"},
            script_path=candidate,
        )

    assert invocations == [[str(candidate)]]


def test_bootstrap_pair_readiness_uses_disabled_cron_and_contiguous_reconciles(
    tmp_path,
    monkeypatch,
):
    installed = tmp_path / "hermes-session-watchdog.py"
    installed.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    installed.chmod(0o700)
    script = {
        "resolved_path": str(installed),
        "sha256": "a" * 64,
        "resolved_mode": 0o700,
        "resolved_uid": os.getuid(),
    }
    disabled = {
        "status": "disabled",
        "crontab_sha256": "b" * 64,
        "marker_sha256": "c" * 64,
    }
    state_before = {"claim_revision": 7, "sha256": "d" * 64}
    state_middle = {"claim_revision": 8, "sha256": "e" * 64}
    state_after = {"claim_revision": 8, "sha256": "f" * 64}
    plan = {
        "watchdog_installed_script": str(installed),
        "watchdog_expected_sha256": script["sha256"],
    }
    prepared = {"watchdog_cron": {"status": "prepared"}}
    phases = {
        "watchdog_installed": {"script": script},
        "watchdog_reconciled_once": {
            "status": "superseded_turn_after_abandoned_dispatch",
            "state_before": state_before,
            "state_after": state_middle,
            "installed_script": script,
        },
        "watchdog_reconciled_twice": {
            "status": "no_reconcilable_slot",
            "state_before": state_middle,
            "state_after": state_after,
            "installed_script": script,
        },
        "watchdog_cron_disabled": disabled,
    }
    monkeypatch.setattr(
        cutover,
        "_file_identity_receipt",
        lambda actual: script
        if Path(actual) == installed
        else pytest.fail("unexpected script"),
    )
    monkeypatch.setattr(
        cutover,
        "_attest_disabled_watchdog_cron",
        lambda actual_plan, actual_prepared: disabled
        if actual_plan is plan and actual_prepared is prepared
        else pytest.fail("wrong disabled cron inputs"),
    )
    monkeypatch.setattr(
        cutover,
        "_cron_watchdog_receipt",
        lambda _plan: pytest.fail("pre-open readiness must not require active cron"),
    )

    receipt = cutover._attest_bootstrap_pair_readiness(
        plan,
        prepared,
        phases,
    )

    assert receipt["status"] == "verified"
    assert receipt["cron"] == disabled
    assert receipt["reconcile_claim_revision"] == 8

    phases["watchdog_reconciled_twice"]["state_before"] = {
        "claim_revision": 8,
        "sha256": "0" * 64,
    }
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="durable watchdog reconciliation proof is invalid",
    ):
        cutover._attest_bootstrap_pair_readiness(
            plan,
            prepared,
            phases,
        )


def test_release_watchdog_barrier_disables_reconciles_and_holds_exact_lock(
    tmp_path,
    monkeypatch,
):
    plan, _candidate = _watchdog_barrier_transaction(tmp_path)
    prepared = {
        "watchdog_cron": {
            "backup_path": str(tmp_path / "crontab.backup"),
            "backup_sha256": "d" * 64,
            "crontab_sha256": "e" * 64,
            "watchdog_command": "* * * * * watchdog",
        }
    }
    expected_state = {
        "path": str(tmp_path / "state.json"),
        "exists": True,
        "sha256": "f" * 64,
        "schema_version": 1,
        "claim_revision": 7,
    }
    lock = object()
    events = []
    monkeypatch.setattr(
        cutover,
        "_disable_watchdog_cron",
        lambda _plan, _prepared: events.append("disable")
        or {"status": "disabled"},
    )
    monkeypatch.setattr(
        cutover,
        "_watchdog_reconcile_receipt",
        lambda _plan, _prepared, **kwargs: events.append(
            ("reconcile", kwargs.get("script_path"))
        )
        or {
            "status": "no_reconcilable_slot",
            "state_before": {**expected_state, "claim_revision": 6},
            "state_after": expected_state,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_acquire_watchdog_state_lock",
        lambda _plan: events.append("acquire") or lock,
    )
    monkeypatch.setattr(
        cutover,
        "_verify_watchdog_state_lock",
        lambda _plan, actual: events.append("verify-lock")
        or {"status": "locked"}
        if actual is lock
        else pytest.fail("wrong lock"),
    )
    monkeypatch.setattr(
        cutover,
        "_watchdog_state_receipt",
        lambda _plan: events.append("state") or expected_state,
    )

    barrier = cutover._begin_release_watchdog_barrier(
        plan,
        prepared=prepared,
    )

    assert barrier["lock"] is lock
    assert barrier["prepared"] == prepared
    assert barrier["state"] == expected_state
    assert events == [
        "disable",
        ("reconcile", plan["watchdog_candidate_script"]),
        "acquire",
        "verify-lock",
        "state",
    ]
    phases = cutover.read_transaction_journal(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
    )["phases"]
    assert phases["watchdog_cron_disable_intent"]["prepared"] == prepared
    assert phases["watchdog_cron_disabled"] == {"status": "disabled"}
    assert phases["watchdog_state_reconciled"]["state_after"] == expected_state


def test_release_watchdog_barrier_readiness_attests_disabled_writer_and_lock(
    monkeypatch,
):
    plan = {
        "watchdog_installed_script": "/tmp/managed-watchdog.py",
        "watchdog_expected_sha256": "a" * 64,
    }
    lock = object()
    prepared = {"watchdog_cron": {"watchdog_command": "managed-watchdog"}}
    disabled = {
        "status": "disabled",
        "crontab_sha256": "b" * 64,
        "marker_sha256": "c" * 64,
    }
    state = {"sha256": "d" * 64, "claim_revision": 9}
    lock_receipt = {"status": "locked", "inode": 41}
    barrier = {
        "status": "held",
        "lock": lock,
        "prepared": prepared,
        "disabled": disabled,
        "state": state,
        "lock_receipt": lock_receipt,
    }
    script = {
        "sha256": plan["watchdog_expected_sha256"],
        "resolved_mode": 0o700,
    }
    events = []
    monkeypatch.setattr(
        cutover,
        "_verify_watchdog_state_lock",
        lambda actual_plan, actual_lock: events.append("verify-lock")
        or lock_receipt
        if actual_plan is plan and actual_lock is lock
        else pytest.fail("wrong barrier lock"),
    )
    monkeypatch.setattr(
        cutover,
        "_watchdog_state_receipt",
        lambda actual_plan: events.append("state") or state
        if actual_plan is plan
        else pytest.fail("wrong plan"),
    )
    monkeypatch.setattr(
        cutover,
        "_attest_disabled_watchdog_cron",
        lambda actual_plan, actual_prepared: events.append("disabled")
        or disabled
        if actual_plan is plan and actual_prepared is prepared
        else pytest.fail("wrong disabled barrier"),
    )
    monkeypatch.setattr(
        cutover,
        "_file_identity_receipt",
        lambda path: events.append("script") or script
        if path == plan["watchdog_installed_script"]
        else pytest.fail("wrong watchdog script"),
    )

    assert cutover._attest_release_watchdog_barrier(plan, barrier) == {
        "status": "verified-disabled-barrier",
        "script": script,
        "cron": disabled,
        "state": state,
        "lock": lock_receipt,
    }
    assert events == ["verify-lock", "state", "disabled", "script"]


def test_release_watchdog_barrier_readiness_rejects_changed_disabled_receipt(
    monkeypatch,
):
    plan = {
        "watchdog_installed_script": "/tmp/managed-watchdog.py",
        "watchdog_expected_sha256": "a" * 64,
    }
    lock = object()
    prepared = {"watchdog_cron": {"watchdog_command": "managed-watchdog"}}
    disabled = {"status": "disabled", "marker_sha256": "b" * 64}
    state = {"sha256": "c" * 64, "claim_revision": 9}
    barrier = {
        "status": "held",
        "lock": lock,
        "prepared": prepared,
        "disabled": disabled,
        "state": state,
        "lock_receipt": {"status": "locked"},
    }
    monkeypatch.setattr(
        cutover,
        "_verify_watchdog_state_lock",
        lambda *_args: barrier["lock_receipt"],
    )
    monkeypatch.setattr(cutover, "_watchdog_state_receipt", lambda _plan: state)
    monkeypatch.setattr(
        cutover,
        "_attest_disabled_watchdog_cron",
        lambda *_args: {**disabled, "marker_sha256": "d" * 64},
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="disabled watchdog scheduler changed while release barrier was held",
    ):
        cutover._attest_release_watchdog_barrier(plan, barrier)


def test_release_watchdog_barrier_rejects_state_change_and_leaves_cron_disabled(
    tmp_path,
    monkeypatch,
):
    plan, _candidate = _watchdog_barrier_transaction(tmp_path)
    prepared = {
        "watchdog_cron": {
            "backup_path": str(tmp_path / "crontab.backup"),
            "backup_sha256": "d" * 64,
            "crontab_sha256": "e" * 64,
            "watchdog_command": "* * * * * watchdog",
        }
    }
    expected_state = {
        "path": str(tmp_path / "state.json"),
        "exists": True,
        "sha256": "f" * 64,
        "schema_version": 1,
        "claim_revision": 7,
    }
    changed_state = {**expected_state, "sha256": "0" * 64, "claim_revision": 8}
    lock = object()
    events = []
    monkeypatch.setattr(
        cutover,
        "_disable_watchdog_cron",
        lambda *_args: {"status": "disabled"},
    )
    monkeypatch.setattr(
        cutover,
        "_watchdog_reconcile_receipt",
        lambda *_args, **_kwargs: {
            "status": "no_reconcilable_slot",
            "state_before": expected_state,
            "state_after": expected_state,
        },
    )
    monkeypatch.setattr(cutover, "_acquire_watchdog_state_lock", lambda _plan: lock)
    monkeypatch.setattr(
        cutover,
        "_verify_watchdog_state_lock",
        lambda _plan, _lock: {"status": "locked"},
    )
    monkeypatch.setattr(cutover, "_watchdog_state_receipt", lambda _plan: changed_state)
    monkeypatch.setattr(
        cutover,
        "_release_watchdog_state_lock",
        lambda _plan, actual: events.append(("release", actual)),
    )
    monkeypatch.setattr(
        cutover,
        "_restore_watchdog_cron",
        lambda *_args: pytest.fail("unsafe state must not restore cron"),
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="watchdog state changed before writer barrier",
    ):
        cutover._begin_release_watchdog_barrier(plan, prepared=prepared)

    assert events == [("release", lock)]
    phases = cutover.read_transaction_journal(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
    )["phases"]
    assert "watchdog_cron_disabled" in phases
    assert "watchdog_cron_restored" not in phases


def _record_webui_pair_commit_phases(
    plan: dict,
    candidate: dict,
    *,
    through: str,
) -> None:
    phases = (
        ("old_fenced", {}),
        ("old_committed", {}),
        ("selection_activated", {}),
        ("old_stopped", {}),
        ("replacement_proved", {"identity": candidate}),
        ("candidate_fenced_health_proved", {"identity": candidate}),
        ("pair_ready", {"status": "ready"}),
        ("pair_gate_install_intent", {"status": "prepared"}),
        ("pair_gate_installed", {"status": "installed"}),
        ("pair_commit_intent", {"status": "committing"}),
        ("promoted", {"status": "promoted"}),
        ("gateway_opened", {"status": "opened"}),
        (
            "candidate_accepted",
            {"identity": candidate, "admission": {"state": "open"}},
        ),
        ("accepted_health_proved", {"status": "healthy"}),
        ("pair_accepted", {"status": "accepted"}),
        ("pair_gate_release_intent", {"status": "prepared"}),
        ("pair_released", {"status": "released"}),
        ("pair_opened", {"status": "opened"}),
    )
    for phase, receipt in phases:
        cutover.record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase=phase,
            receipt=receipt,
        )
        if phase == through:
            return
    raise AssertionError(f"unknown pair phase: {through}")


def test_release_transaction_resumes_release_intent_without_repreparing_pair(
    tmp_path,
):
    transaction_id = "released-pair-resume-transaction-000001"
    expected_candidate = {
        "build_id": "candidate",
        "manifest_sha256": "a" * 64,
        "agent_manifest_sha256": "b" * 64,
        "runtime_manifest_sha256": "c" * 64,
        "selector_generation": 2,
        "release_path": "/immutable/releases/candidate",
        "launchd_label": "com.example.webui",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    candidate_identity = {
        **expected_candidate,
        "pid": 202,
        "pid_start_token": "candidate-start",
    }
    journal_path = tmp_path / "release-intent-resume.json"
    cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=expected_candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "d" * 64,
            "state_snapshot_id": "snapshot-before-candidate",
            "state_snapshot_sha256": "f" * 64,
        },
    )
    durable_phases = (
        ("staged", {"generation": 1}),
        ("plist_installed", {"plist_sha256": "e" * 64}),
        ("old_fenced", {"identity": {"pid": 101}}),
        ("old_committed", {"identity": {"pid": 101}}),
        ("selection_activated", {"selection": {"generation": 2}}),
        ("old_stopped", {"identity": {"pid": 101}}),
        ("replacement_proved", {"identity": candidate_identity}),
        ("candidate_fenced_health_proved", {"identity": candidate_identity}),
        ("pair_ready", {"pair": {"status": "ready"}}),
        ("pair_gate_install_intent", {"status": "prepared"}),
        (
            "pair_gate_installed",
            {"owner_hash": "1" * 64, "payload_sha256": "2" * 64},
        ),
        ("pair_commit_intent", {"build_id": "candidate"}),
        ("promoted", {"promotion": {"generation": 3}}),
        ("gateway_opened", {"gateway": {"status": "opened"}}),
        (
            "candidate_accepted",
            {"identity": candidate_identity, "admission": {"state": "open"}},
        ),
        ("accepted_health_proved", {"identity": candidate_identity}),
        ("pair_accepted", {"identity": candidate_identity}),
        (
            "pair_gate_release_intent",
            {"owner_hash": "1" * 64, "payload_sha256": "2" * 64},
        ),
    )
    for phase, receipt in durable_phases:
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipt,
        )

    open_inspection = {
        "status": "inspected",
        "transaction_id": transaction_id,
        "identity": candidate_identity,
        "admission": {"state": "open", "transaction_id": None},
    }
    accepted_health = {
        "status": "verified",
        "launchd_pid": 202,
        "listener_pid": 202,
        "signed_health_pid": 202,
        "pid_start_token": "candidate-start",
        "deep_health": {
            "status": "ok",
            "build": {
                "status": "managed",
                "valid": True,
                **{
                    key: expected_candidate[key]
                    for key in (
                        "build_id",
                        "manifest_sha256",
                        "agent_manifest_sha256",
                        "runtime_manifest_sha256",
                        "selector_generation",
                    )
                },
            },
            "admission": {"state": "open"},
            "checks": {
                name: {"status": "ok"}
                for name in (
                    "streams_lock",
                    "stream_runtime",
                    "sessions",
                    "projects",
                    "state_db",
                )
            },
        },
    }
    lifecycle = []

    result = cutover.run_release_control_cutover(
        initial_inspection=open_inspection,
        inspect_control=lambda: open_inspection,
        send_control=lambda *_args: pytest.fail("accepted candidate is already open"),
        attest_selector_state=lambda: {
            "status": "verified",
            "transaction_id": transaction_id,
            "current": "candidate",
            "candidate": None,
            "pending_transaction_id": None,
        },
        attest_installed_plist=lambda: {
            "status": "verified",
            "launchd_label": "com.example.webui",
            "plist_sha256": "e" * 64,
        },
        activate_selection=lambda: pytest.fail("selector is already activated"),
        promote_selection=lambda: pytest.fail("selector is already promoted"),
        rollback_selection=lambda: pytest.fail("rollback is forbidden"),
        restore_plist=lambda: pytest.fail("rollback is forbidden"),
        stop_failed_candidate=lambda: pytest.fail("rollback is forbidden"),
        restore_state_snapshot=lambda: pytest.fail("rollback is forbidden"),
        restart_selection=lambda: pytest.fail("rollback is forbidden"),
        verify_rollback=lambda: pytest.fail("rollback is forbidden"),
        signal_process=lambda _identity: pytest.fail("old process is already stopped"),
        wait_for_process_exit=lambda *_args: pytest.fail(
            "old process is already stopped"
        ),
        inspect_candidate_binding=lambda _identity: accepted_health,
        inspect_accepted_binding=lambda _identity: accepted_health,
        prepare_pair_before_commit=lambda _identity: pytest.fail(
            "durably released pair must not be prepared again"
        ),
        pair_gate_intent_before_commit=lambda *_args: pytest.fail(
            "pair-gate intent is already durable"
        ),
        install_pair_gate_before_commit=lambda *_args: pytest.fail(
            "released pair gate must not be reinstalled"
        ),
        open_pair_after_promotion=lambda _identity: pytest.fail(
            "gateway is already open"
        ),
        release_pair_after_acceptance=lambda _identity, intent: (
            lifecycle.append(("release", intent))
            or {
                "release": {"status": "already-released"},
                "opened": {"status": "verified"},
            }
        ),
        expected_candidate_identity=expected_candidate,
        transaction_id=transaction_id,
        transaction_journal_path=journal_path,
        timeout_seconds=2,
        interval_seconds=0.01,
    )

    assert result["status"] == "accepted"
    assert lifecycle == [
        (
            "release",
            {"owner_hash": "1" * 64, "payload_sha256": "2" * 64},
        )
    ]
    phases = cutover.read_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
    )["phases"]
    assert phases["pair_released"] == {"status": "already-released"}
    assert phases["pair_opened"] == {"status": "verified"}


def test_release_watchdog_barrier_restores_cron_under_lock_after_pair_opened(
    tmp_path,
    monkeypatch,
):
    plan, candidate = _watchdog_barrier_transaction(tmp_path)
    prepared = {
        "watchdog_cron": {
            "backup_path": str(tmp_path / "crontab.backup"),
            "backup_sha256": "d" * 64,
            "crontab_sha256": "e" * 64,
            "watchdog_command": "* * * * * watchdog",
        }
    }
    state = {"sha256": "f" * 64, "claim_revision": 7}
    lock = object()
    for phase, receipt in (
        ("watchdog_cron_disable_intent", {"prepared": prepared}),
        ("watchdog_cron_disabled", {"status": "disabled"}),
        (
            "watchdog_state_reconciled",
            {"status": "no_reconcilable_slot", "state_after": state},
        ),
    ):
        cutover.record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase=phase,
            receipt=receipt,
        )
    _record_webui_pair_commit_phases(plan, candidate, through="pair_opened")
    disabled = {
        "status": "disabled",
        "crontab_sha256": "a" * 64,
        "marker_sha256": "b" * 64,
    }
    barrier = {
        "lock": lock,
        "prepared": prepared,
        "disabled": disabled,
        "state": state,
    }
    events = []
    monkeypatch.setattr(
        cutover,
        "_verify_watchdog_state_lock",
        lambda _plan, actual: events.append("verify-lock")
        or {"status": "locked"}
        if actual is lock
        else pytest.fail("wrong lock"),
    )
    monkeypatch.setattr(
        cutover,
        "_watchdog_state_receipt",
        lambda _plan: events.append("state") or state,
    )
    monkeypatch.setattr(
        cutover,
        "_restore_watchdog_cron",
        lambda _plan, _prepared: events.append("restore")
        or {"crontab_sha256": "e" * 64, "watchdog_command": "* * * * * watchdog"},
    )
    monkeypatch.setattr(
        cutover,
        "_release_watchdog_state_lock",
        lambda _plan, actual: events.append("release")
        or {"status": "released"}
        if actual is lock
        else pytest.fail("wrong lock"),
    )

    result = cutover._finish_release_watchdog_barrier(plan, barrier)

    assert result["status"] == "restored-after-pair-opened"
    assert events == ["verify-lock", "state", "restore", "release"]
    phases = cutover.read_transaction_journal(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
    )["phases"]
    assert phases["watchdog_cron_restored"]["watchdog_command"].endswith(
        "watchdog"
    )
    assert phases["watchdog_cron_restore_intent"]["status"] == "prepared"


def test_release_watchdog_barrier_keeps_cron_disabled_after_pair_commit_failure(
    tmp_path,
    monkeypatch,
):
    plan, candidate = _watchdog_barrier_transaction(tmp_path)
    prepared = {
        "watchdog_cron": {
            "backup_path": str(tmp_path / "crontab.backup"),
            "backup_sha256": "d" * 64,
            "crontab_sha256": "e" * 64,
            "watchdog_command": "* * * * * watchdog",
        }
    }
    disabled = {"status": "disabled"}
    state = {"sha256": "f" * 64, "claim_revision": 7}
    for phase, receipt in (
        ("watchdog_cron_disable_intent", {"prepared": prepared}),
        ("watchdog_cron_disabled", disabled),
        (
            "watchdog_state_reconciled",
            {"status": "no_reconcilable_slot", "state_after": state},
        ),
    ):
        cutover.record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase=phase,
            receipt=receipt,
        )
    _record_webui_pair_commit_phases(plan, candidate, through="promoted")
    lock = object()
    barrier = {
        "lock": lock,
        "prepared": prepared,
        "disabled": disabled,
        "state": state,
    }
    events = []
    monkeypatch.setattr(
        cutover,
        "_verify_watchdog_state_lock",
        lambda *_args: {"status": "locked"},
    )
    monkeypatch.setattr(cutover, "_watchdog_state_receipt", lambda _plan: state)
    monkeypatch.setattr(
        cutover,
        "_attest_disabled_watchdog_cron",
        lambda _plan, _prepared: events.append("attest-disabled") or disabled,
    )
    monkeypatch.setattr(
        cutover,
        "_restore_watchdog_cron",
        lambda *_args: pytest.fail("pair-commit failure must not restore cron"),
    )
    monkeypatch.setattr(
        cutover,
        "_release_watchdog_state_lock",
        lambda _plan, actual: events.append("release")
        or {"status": "released"}
        if actual is lock
        else pytest.fail("wrong lock"),
    )

    result = cutover._finish_release_watchdog_barrier(plan, barrier)

    assert result["status"] == "disabled-for-roll-forward"
    assert events == ["attest-disabled", "release"]
    phases = cutover.read_transaction_journal(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
    )["phases"]
    assert "watchdog_cron_restored" not in phases


def test_release_watchdog_barrier_adopts_exact_snapshot_rollback_state(
    tmp_path,
    monkeypatch,
):
    plan, _candidate = _watchdog_barrier_transaction(tmp_path)
    snapshot = {
        "manifest_path": str(tmp_path / "snapshot-manifest.json"),
        "state_snapshot_id": "snapshot-before-candidate",
        "state_snapshot_sha256": "a" * 64,
    }
    restored = {
        "status": "restored",
        "state_snapshot_id": snapshot["state_snapshot_id"],
        "state_snapshot_sha256": snapshot["state_snapshot_sha256"],
    }
    verified = {
        "status": "verified",
        "state_snapshot_id": snapshot["state_snapshot_id"],
        "state_snapshot_sha256": snapshot["state_snapshot_sha256"],
    }
    cron = {"status": "restored", "crontab_sha256": "b" * 64}
    phases = {
        "paired_state_snapshot_created": snapshot,
        "rollback_started": {"error_type": "ReleaseBuildError"},
        "state_snapshot_restored": restored,
        "rollback_verified": verified,
        "watchdog_cron_rollback_restored": cron,
    }
    lock = object()
    expected_state = {"sha256": "c" * 64, "claim_revision": 9}
    rollback_state = {"sha256": "d" * 64, "claim_revision": 7}
    barrier = {
        "lock": lock,
        "prepared": {"watchdog_cron": {"status": "prepared"}},
        "disabled": {"status": "disabled"},
        "state": expected_state,
    }
    events = []
    monkeypatch.setattr(
        cutover,
        "_verify_watchdog_state_lock",
        lambda _plan, actual: {"status": "locked"}
        if actual is lock
        else pytest.fail("wrong lock"),
    )
    monkeypatch.setattr(
        cutover,
        "_watchdog_state_receipt",
        lambda _plan: rollback_state,
    )
    monkeypatch.setattr(
        cutover,
        "read_transaction_journal",
        lambda *_args, **_kwargs: {"phases": phases},
    )
    monkeypatch.setattr(
        cutover,
        "_read_verified_state_snapshot",
        lambda manifest_path, **kwargs: events.append(
            (manifest_path, kwargs)
        )
        or ({}, {"status": "verified"}),
    )
    monkeypatch.setattr(
        cutover,
        "_restore_watchdog_cron",
        lambda *_args: cron,
    )
    monkeypatch.setattr(
        cutover,
        "_release_watchdog_state_lock",
        lambda _plan, actual: {"status": "released"}
        if actual is lock
        else pytest.fail("wrong lock"),
    )

    result = cutover._finish_release_watchdog_barrier(plan, barrier)

    assert result["status"] == "restored-after-rollback"
    assert result["state"]["status"] == "restored-by-exact-rollback"
    assert result["state"]["before"] == expected_state
    assert result["state"]["after"] == rollback_state
    assert events == [
        (
            snapshot["manifest_path"],
            {
                "expected_snapshot_id": snapshot["state_snapshot_id"],
                "expected_manifest_sha256": snapshot[
                    "state_snapshot_sha256"
                ],
                "live": True,
            },
        )
    ]


@pytest.mark.parametrize("external_drain", [False, True])
def test_release_control_driver_commits_pair_before_sequential_open(
    tmp_path,
    external_drain,
):
    transaction_id = "release-transaction-00000000000001"
    old_identity = {
        "pid": 123,
        "pid_start_token": "old-start",
        "instance_id": "instance-a",
    }
    expected_candidate = {
        "build_id": "candidate",
        "manifest_sha256": "a" * 64,
        "agent_manifest_sha256": "b" * 64,
        "runtime_manifest_sha256": "c" * 64,
        "selector_generation": 2,
        "release_path": "/immutable/releases/candidate",
        "launchd_label": "com.example.webui",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    candidate_identity = {
        **expected_candidate,
        "pid": 456,
        "pid_start_token": "candidate-start",
        "instance_id": "instance-b",
    }
    journal_path = tmp_path / "transactions" / "release.json"
    cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=expected_candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "d" * 64,
            "state_snapshot_id": "snapshot-before-candidate",
            "state_snapshot_sha256": "f" * 64,
        },
    )
    cutover.record_transaction_phase(
        journal_path,
        transaction_id=transaction_id,
        phase="staged",
        receipt={"build_id": "candidate", "generation": 1},
    )
    cutover.record_transaction_phase(
        journal_path,
        transaction_id=transaction_id,
        phase="plist_installed",
        receipt={"plist_sha256": "e" * 64},
    )
    drained = {
        "status": "inspected",
        "transaction_id": transaction_id,
        "identity": old_identity,
        "admission": {
            "state": "fenced",
            "reservations": 0,
            "active_runs": 0,
        },
        "activity": {
            "active_streams": 0,
            "active_async_delegations": 0,
            "async_delegations_available": True,
            "active_background_memory_commits": 0,
            "in_flight_memory_commits": 0,
            "memory_commit_activity_available": True,
            "pending_oauth_flows": 0,
            "oauth_activity_available": True,
            "active_terminals": 0,
            "terminal_activity_available": True,
            "running_processes": 0,
            "finalizing_processes": 0,
            "durable_undelivered_completions": 0,
            "process_completion_activity_available": True,
        },
    }
    if external_drain:
        drained["activity"] = {
            key: value
            for key, value in drained["activity"].items()
            if key
            not in {
                "running_processes",
                "finalizing_processes",
                "durable_undelivered_completions",
            }
        }
        drained["activity"]["process_completion_activity_available"] = False
    inspection_rows = iter(
        [
            {
                "status": "inspected",
                "transaction_id": transaction_id,
                "identity": old_identity,
                **{key: value for key, value in drained.items() if key != "identity"},
            },
            {
                "status": "inspected",
                "transaction_id": transaction_id,
                "identity": candidate_identity,
                "admission": {
                    "state": "startup-fenced",
                    "lease_expires_at": None,
                    "transaction_id": transaction_id,
                    "startup_error": None,
                },
            },
            {
                "status": "inspected",
                "transaction_id": transaction_id,
                "identity": candidate_identity,
                "admission": {"state": "open", "transaction_id": None},
            },
        ]
    )
    actions = []
    external_drain_calls = []

    def attest_external_drain(exact, inspection):
        external_drain_calls.append((exact, inspection))
        return {
            "status": "verified",
            "identity": exact,
            "proof": "exact-external-process-barrier",
        }

    def send_control(action, expected, fence_token=None):
        actions.append((action, expected, fence_token))
        if action == "fence" and expected == old_identity:
            return {
                "status": "fenced",
                "transaction_id": transaction_id,
                "fence_token": "old-secret-token",
                "identity": old_identity,
            }
        if action == "commit":
            assert external_drain is False
            assert fence_token == "old-secret-token"
            return {
                "status": "committing",
                "transaction_id": transaction_id,
                "identity": old_identity,
                "admission": {"state": "committing"},
            }
        if action == "fence":
            assert expected == candidate_identity
            return {
                "status": "startup-fenced",
                "transaction_id": transaction_id,
                "fence_token": "candidate-secret-token",
                "identity": candidate_identity,
                "admission": {
                    "state": "startup-fenced",
                    "lease_expires_at": None,
                    "transaction_id": transaction_id,
                    "startup_error": None,
                },
            }
        assert action == "accept"
        assert expected == candidate_identity
        assert fence_token == "candidate-secret-token"
        lifecycle.append("webui-accepted")
        return {
            "status": "accepted",
            "transaction_id": transaction_id,
            "identity": candidate_identity,
            "admission": {"state": "open", "transaction_id": None},
        }

    lifecycle = []
    deep_health_calls = []
    clock = {"now": 0.0}

    def inspect_deep_candidate(_identity):
        state = "startup-fenced" if not deep_health_calls else "open"
        deep_health_calls.append(state)
        lifecycle.append(f"candidate-{state}-health")
        checks = {
            "streams_lock": {"status": "ok"},
            "stream_runtime": {"status": "ok"},
        }
        if state == "startup-fenced":
            checks.update(
                {
                    "startup_fence": {
                        "status": "fenced",
                        "mutation_free": True,
                    },
                    "sessions": {"status": "deferred"},
                    "projects": {"status": "deferred"},
                    "state_db": {"status": "deferred"},
                }
            )
        else:
            checks.update(
                {
                    "sessions": {"status": "ok"},
                    "projects": {"status": "ok"},
                    "state_db": {"status": "ok"},
                }
            )
        return {
            "status": "verified",
            "launchd_pid": 456,
            "listener_pid": 456,
            "signed_health_pid": 456,
            "pid_start_token": "candidate-start",
            "deep_health": {
                "status": "ok",
                "build": {
                    "status": "managed",
                    "valid": True,
                    **{
                        key: expected_candidate[key]
                        for key in (
                            "build_id",
                            "manifest_sha256",
                            "agent_manifest_sha256",
                            "runtime_manifest_sha256",
                            "selector_generation",
                        )
                    },
                },
                "admission": {"state": state},
                "checks": checks,
            },
        }

    def bootstrap_candidate():
        lifecycle.append("candidate-bootstrapped")
        # Gateway preparation is a separate bounded phase and may consume the
        # original drain window before WebUI launch.  The replacement must
        # still receive its own readiness window after bootstrap completes.
        clock["now"] = 3.0
        return {"status": "started"}

    result = cutover.run_release_control_cutover(
        initial_inspection={
            "status": "inspected",
            "transaction_id": transaction_id,
            "identity": old_identity,
        },
        inspect_control=lambda: next(inspection_rows),
        send_control=send_control,
        attest_selector_state=lambda: {
            "status": "verified",
            "transaction_id": transaction_id,
            "current": "last-good",
            "candidate": "candidate",
            "pending_transaction_id": transaction_id,
        },
        attest_installed_plist=lambda: {
            "status": "verified",
            "launchd_label": "com.example.webui",
            "plist_sha256": "e" * 64,
        },
        activate_selection=lambda: lifecycle.append("activated") or {"generation": 2},
        promote_selection=lambda: lifecycle.append("promoted") or {"generation": 3},
        rollback_selection=lambda: lifecycle.append("rolled-back"),
        restore_plist=lambda: lifecycle.append("plist-restored"),
        stop_failed_candidate=lambda: lifecycle.append("candidate-stopped"),
        restore_state_snapshot=lambda: lifecycle.append("state-restored"),
        restart_selection=lambda: lifecycle.append("restarted"),
        verify_rollback=lambda: {"status": "verified"},
        signal_process=lambda exact: lifecycle.append(("signalled", exact)),
        wait_for_process_exit=lambda exact, timeout: lifecycle.append(
            ("exited", exact, timeout)
        ),
        bootstrap_candidate_job=bootstrap_candidate,
        inspect_candidate_binding=lambda _identity: lifecycle.append(
            "candidate-binding"
        ) or {
            "status": "verified",
            "launchd_pid": 456,
            "listener_pid": 456,
            "signed_health_pid": 456,
            "pid_start_token": "candidate-start",
            "deep_health": {
                "status": "ok",
                "build": {
                    "status": "managed",
                    "valid": True,
                    **{
                        key: expected_candidate[key]
                        for key in (
                            "build_id",
                            "manifest_sha256",
                            "agent_manifest_sha256",
                            "runtime_manifest_sha256",
                            "selector_generation",
                        )
                    },
                },
                "admission": {"state": "startup-fenced"},
            },
        },
        inspect_accepted_binding=inspect_deep_candidate,
        prepare_pair_before_commit=lambda _identity: lifecycle.append(
            "pair-ready"
        ) or {"status": "ready"},
        open_pair_after_promotion=lambda _identity: lifecycle.append(
            "gateway-opened"
        ) or {"status": "opened"},
        attest_legacy_activity_drain=(
            attest_external_drain if external_drain else None
        ),
        expected_candidate_identity=expected_candidate,
        transaction_id=transaction_id,
        transaction_journal_path=journal_path,
        timeout_seconds=2,
        interval_seconds=0.01,
        monotonic=lambda: clock["now"],
    )

    expected_actions = ["fence", "fence", "accept"]
    if not external_drain:
        expected_actions.insert(1, "commit")
    assert [row[0] for row in actions] == expected_actions
    assert len(external_drain_calls) == int(external_drain)
    assert result["status"] == "accepted"
    assert result["identity"] == candidate_identity
    assert lifecycle[0] == "activated"
    assert lifecycle[1] == ("signalled", old_identity)
    assert lifecycle[2][0:2] == ("exited", old_identity)
    assert lifecycle[3:] == [
        "candidate-bootstrapped",
        "candidate-binding",
        "candidate-startup-fenced-health",
        "pair-ready",
        "promoted",
        "gateway-opened",
        "webui-accepted",
        "candidate-open-health",
    ]
    assert "secret-token" not in json.dumps(result)
    journal = cutover.read_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
    )
    if external_drain:
        assert journal["phases"]["old_committed"][
            "external_activity_drain"
        ]["proof"] == "exact-external-process-barrier"
    else:
        assert "external_activity_drain" not in journal["phases"][
            "old_committed"
        ]
    assert set(journal["phases"]) == {
        "staged",
        "plist_installed",
        "old_fenced",
        "old_committed",
        "selection_activated",
        "old_stopped",
        "candidate_job_bootstrapped",
        "replacement_proved",
        "candidate_fenced_health_proved",
        "pair_ready",
        "pair_commit_intent",
        "promoted",
        "gateway_opened",
        "candidate_accepted",
        "accepted_health_proved",
        "pair_accepted",
    }


def test_legacy_webui_activity_drain_uses_exact_locked_durable_zero(
    tmp_path,
    monkeypatch,
):
    identity = {
        "build_id": "last-good",
        "pid": 123,
        "pid_start_token": "old-start",
    }
    plan = {
        "last_good_identity": {"build_id": "last-good"},
        "synthetic_process_notifications_path": str(
            tmp_path / "process_notifications.json"
        ),
    }
    inspection = {
        "status": "inspected",
        "identity": identity,
        "admission": {
            "state": "fenced",
            "active_runs": 0,
            "reservations": 0,
        },
        "activity": {
            "active_streams": 0,
            "active_async_delegations": 0,
            "async_delegations_available": True,
            "active_background_memory_commits": 0,
            "in_flight_memory_commits": 0,
            "memory_commit_activity_available": True,
            "pending_oauth_flows": 0,
            "oauth_activity_available": True,
            "active_terminals": 0,
            "terminal_activity_available": True,
            "process_completion_activity_available": False,
        },
    }
    events = []

    def acquire(_plan, *, kind):
        events.append(("acquire", kind))
        return {
            "kind": kind,
            "handle": object(),
            "receipt": {"kind": kind, "inode": len(events)},
        }

    monkeypatch.setattr(cutover, "_acquire_process_registry_lock", acquire)
    monkeypatch.setattr(
        cutover,
        "_release_process_registry_lock",
        lambda _plan, held: events.append(("release", held["kind"]))
        or {"status": "released", "kind": held["kind"]},
    )
    monkeypatch.setattr(
        cutover,
        "_legacy_durable_activity_receipt",
        lambda _plan: {
            "status": "verified",
            "webui_active_run_leases": 0,
            "gateway_process_checkpoint": {"active_records": 0},
        },
    )
    monkeypatch.setattr(
        cutover,
        "_read_synthetic_store_receipt",
        lambda *_args, **_kwargs: (
            {"status": "present", "sha256": "a" * 64},
            {"version": 1, "events": {}},
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_attest_managed_gateway_binding",
        lambda *_args, **_kwargs: {
            "status": "verified",
            "listener_pid": 456,
            "pid_start_token": "gateway-start",
            "build_id": "last-good",
        },
    )

    receipt = cutover._attest_legacy_webui_activity_drain(
        plan,
        identity,
        inspection,
        inspect_control=lambda: inspection,
    )

    assert receipt["status"] == "verified"
    assert receipt["identity"] == identity
    assert receipt["outbox"]["undelivered"] == 0
    assert events == [
        ("acquire", "admission"),
        ("acquire", "completion"),
        ("acquire", "authority"),
        ("release", "authority"),
        ("release", "completion"),
        ("release", "admission"),
    ]
    busy = {
        **inspection,
        "activity": {**inspection["activity"], "active_streams": 1},
    }
    assert (
        cutover._attest_legacy_webui_activity_drain(
            plan,
            identity,
            busy,
            inspect_control=lambda: busy,
        )
        is None
    )
    events.clear()
    monkeypatch.setattr(
        cutover,
        "_read_synthetic_store_receipt",
        lambda *_args, **_kwargs: (
            {"status": "present", "sha256": "b" * 64},
            {
                "version": 1,
                "events": {
                    "process:busy:completion": {
                        "event_id": "process:busy:completion",
                        "delivered": False,
                    }
                },
            },
        ),
    )
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="undelivered work",
    ):
        cutover._attest_legacy_webui_activity_drain(
            plan,
            identity,
            inspection,
            inspect_control=lambda: inspection,
        )
    assert events == [
        ("acquire", "admission"),
        ("acquire", "completion"),
        ("acquire", "authority"),
        ("release", "authority"),
        ("release", "completion"),
        ("release", "admission"),
    ]


@pytest.mark.parametrize(
    "resume_phase,expected_old_commit,expected_activate,expected_accept,expected_promote",
    [
        ("old_fenced", 1, 1, 1, 1),
        ("old_committed", 0, 1, 1, 1),
        ("selection_activated", 0, 0, 1, 1),
        ("old_stopped", 0, 0, 1, 1),
        ("replacement_proved", 0, 0, 1, 1),
        ("candidate_fenced_health_proved", 0, 0, 1, 1),
        ("pair_ready", 0, 0, 1, 1),
        ("pair_commit_intent", 0, 0, 1, 1),
        ("promoted", 0, 0, 1, 0),
        ("gateway_opened", 0, 0, 1, 0),
        ("candidate_accepted", 0, 0, 0, 0),
        ("accepted_health_proved", 0, 0, 0, 0),
        ("pair_accepted", 0, 0, 0, 0),
    ],
)
def test_release_transaction_resumes_from_each_durable_external_phase(
    tmp_path,
    resume_phase,
    expected_old_commit,
    expected_activate,
    expected_accept,
    expected_promote,
):
    transaction_id = "resume-transaction-000000000000001"
    old_identity = {"pid": 101, "pid_start_token": "old-start"}
    expected_candidate = {
        "build_id": "candidate",
        "manifest_sha256": "a" * 64,
        "agent_manifest_sha256": "b" * 64,
        "runtime_manifest_sha256": "c" * 64,
        "selector_generation": 2,
        "release_path": "/immutable/releases/candidate",
        "launchd_label": "com.example.webui",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    candidate_identity = {
        **expected_candidate,
        "pid": 202,
        "pid_start_token": "candidate-start",
    }
    journal_path = tmp_path / f"{resume_phase}.json"
    cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=expected_candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "d" * 64,
            "state_snapshot_id": "snapshot-before-candidate",
            "state_snapshot_sha256": "f" * 64,
        },
    )
    phase_order = [
        "staged",
        "plist_installed",
        "old_fenced",
        "old_committed",
        "selection_activated",
        "old_stopped",
        "replacement_proved",
        "candidate_fenced_health_proved",
        "pair_ready",
        "pair_commit_intent",
        "promoted",
        "gateway_opened",
        "candidate_accepted",
        "accepted_health_proved",
        "pair_accepted",
    ]
    receipts = {
        "staged": {"generation": 1},
        "plist_installed": {"plist_sha256": "e" * 64},
        "old_fenced": {"identity": old_identity},
        "old_committed": {"identity": old_identity},
        "selection_activated": {"selection": {"generation": 2}},
        "old_stopped": {"identity": old_identity},
        "replacement_proved": {"identity": candidate_identity},
        "candidate_fenced_health_proved": {"identity": candidate_identity},
        "pair_ready": {"pair": {"status": "ready"}},
        "pair_commit_intent": {"build_id": "candidate"},
        "promoted": {"promotion": {"generation": 3}},
        "gateway_opened": {"gateway": {"status": "opened"}},
        "candidate_accepted": {
            "identity": candidate_identity,
            "admission": {"state": "open"},
        },
        "accepted_health_proved": {"identity": candidate_identity},
        "pair_accepted": {"identity": candidate_identity},
    }
    for phase in phase_order:
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipts[phase],
        )
        if phase == resume_phase:
            break

    candidate_open = resume_phase in {
        "candidate_accepted",
        "accepted_health_proved",
        "pair_accepted",
    }
    initial_identity = (
        old_identity
        if resume_phase in {"old_fenced", "old_committed", "selection_activated"}
        else candidate_identity
    )
    initial = {
        "status": "inspected",
        "transaction_id": transaction_id,
        "identity": initial_identity,
        "admission": (
            {"state": "open", "transaction_id": None}
            if candidate_open
            else {
                "state": "startup-fenced",
                "lease_expires_at": None,
                "transaction_id": transaction_id,
                "startup_error": None,
            }
        ),
    }
    drained_old = {
        "status": "inspected",
        "transaction_id": transaction_id,
        "identity": old_identity,
        "admission": {"state": "fenced", "reservations": 0, "active_runs": 0},
        "activity": {
            "active_streams": 0,
            "active_async_delegations": 0,
            "async_delegations_available": True,
            "active_background_memory_commits": 0,
            "in_flight_memory_commits": 0,
            "memory_commit_activity_available": True,
            "pending_oauth_flows": 0,
            "oauth_activity_available": True,
            "active_terminals": 0,
            "terminal_activity_available": True,
            "running_processes": 0,
            "finalizing_processes": 0,
            "durable_undelivered_completions": 0,
            "process_completion_activity_available": True,
        },
    }
    candidate_startup = {
        "status": "inspected",
        "transaction_id": transaction_id,
        "identity": candidate_identity,
        "admission": {
            "state": "startup-fenced",
            "lease_expires_at": None,
            "transaction_id": transaction_id,
            "startup_error": None,
        },
    }
    candidate_open_receipt = {
        "status": "inspected",
        "transaction_id": transaction_id,
        "identity": candidate_identity,
        "admission": {"state": "open", "transaction_id": None},
    }
    queued = []
    if resume_phase == "old_fenced":
        queued.append(drained_old)
    if resume_phase == "old_committed":
        committed_old = copy.deepcopy(drained_old)
        committed_old["admission"]["state"] = "committing"
        queued.append(committed_old)
    if initial_identity == old_identity:
        queued.append(candidate_startup)
    queued.append(candidate_open_receipt)
    inspections = iter(queued)
    counters = {"old_commit": 0, "activate": 0, "accept": 0, "promote": 0}

    def send_control(action, expected, token=None):
        if action == "fence" and expected == old_identity:
            return {
                "status": (
                    "committing" if resume_phase == "old_committed" else "fenced"
                ),
                "transaction_id": transaction_id,
                "identity": old_identity,
                "fence_token": "old-token",
            }
        if action == "commit":
            counters["old_commit"] += 1
            return {
                "status": "committing",
                "transaction_id": transaction_id,
                "identity": old_identity,
            }
        if action == "fence":
            return {
                "status": "startup-fenced",
                "transaction_id": transaction_id,
                "identity": candidate_identity,
                "fence_token": "candidate-token",
                "admission": candidate_startup["admission"],
            }
        if action == "accept":
            counters["accept"] += 1
            return {
                "status": "accepted",
                "transaction_id": transaction_id,
                "identity": candidate_identity,
                "admission": {"state": "open"},
            }
        raise AssertionError(f"unexpected action: {action}")

    preaccepted_health = {
        "status": "verified",
        "launchd_pid": 202,
        "listener_pid": 202,
        "signed_health_pid": 202,
        "pid_start_token": "candidate-start",
        "deep_health": {
            "status": "ok",
            "build": {"status": "managed", "valid": True, **{
                key: expected_candidate[key]
                for key in (
                    "build_id",
                    "manifest_sha256",
                    "agent_manifest_sha256",
                    "runtime_manifest_sha256",
                    "selector_generation",
                )
            }},
            "admission": {"state": "startup-fenced"},
        },
    }
    preaccepted_health["deep_health"]["checks"] = {
        "streams_lock": {"status": "ok"},
        "stream_runtime": {"status": "ok"},
        "startup_fence": {
            "status": "fenced",
            "mutation_free": True,
        },
        "sessions": {"status": "deferred"},
        "projects": {"status": "deferred"},
        "state_db": {"status": "deferred"},
    }
    accepted_health = copy.deepcopy(preaccepted_health)
    accepted_health["deep_health"]["admission"] = {"state": "open"}
    accepted_health["deep_health"]["checks"] = {
        name: {"status": "ok"}
        for name in (
            "streams_lock",
            "stream_runtime",
            "sessions",
            "projects",
            "state_db",
        )
    }
    if phase_order.index(resume_phase) >= phase_order.index("promoted"):
        selector_attestation = {
            "current": "candidate",
            "candidate": None,
            "pending_transaction_id": None,
        }
    elif phase_order.index(resume_phase) >= phase_order.index(
        "selection_activated"
    ):
        selector_attestation = {
            "current": "candidate",
            "candidate": "candidate",
            "pending_transaction_id": transaction_id,
        }
    else:
        selector_attestation = {
            "current": "last-good",
            "candidate": "candidate",
            "pending_transaction_id": transaction_id,
        }

    deep_health_calls = []

    def inspect_deep_candidate(_identity):
        if candidate_open:
            return accepted_health
        deep_health_calls.append("called")
        return preaccepted_health if len(deep_health_calls) == 1 else accepted_health

    result = cutover.run_release_control_cutover(
        initial_inspection=initial,
        inspect_control=lambda: next(inspections),
        send_control=send_control,
        attest_selector_state=lambda: {
            "status": "verified",
            "transaction_id": transaction_id,
            **selector_attestation,
        },
        attest_installed_plist=lambda: {
            "status": "verified",
            "launchd_label": "com.example.webui",
            "plist_sha256": "e" * 64,
        },
        activate_selection=lambda: counters.__setitem__(
            "activate", counters["activate"] + 1
        )
        or {"generation": 2},
        promote_selection=lambda: counters.__setitem__(
            "promote", counters["promote"] + 1
        )
        or {"generation": 3},
        rollback_selection=lambda: None,
        restore_plist=lambda: None,
        stop_failed_candidate=lambda: None,
        restore_state_snapshot=lambda: None,
        restart_selection=lambda: None,
        verify_rollback=lambda: {"status": "verified"},
        signal_process=lambda _identity: None,
        wait_for_process_exit=lambda _identity, _timeout: None,
        inspect_candidate_binding=lambda _identity: preaccepted_health,
        inspect_accepted_binding=inspect_deep_candidate,
        expected_candidate_identity=expected_candidate,
        transaction_id=transaction_id,
        transaction_journal_path=journal_path,
        timeout_seconds=2,
        interval_seconds=0.01,
    )

    assert result["status"] == "accepted"
    assert counters == {
        "old_commit": expected_old_commit,
        "activate": expected_activate,
        "accept": expected_accept,
        "promote": expected_promote,
    }


@pytest.mark.parametrize(
    "failure_point",
    [
        "attestation",
        "inspect",
        "fence",
        "old_fenced",
        "old_committed",
        "rollback_started",
    ],
)
def test_release_early_failure_matrix_always_restores_selector_and_plist(
    tmp_path,
    failure_point,
):
    transaction_id = "early-rollback-transaction-00000001"
    old_identity = {
        "pid": 101,
        "pid_start_token": "old-start-token",
        "instance_id": "old-instance",
    }
    expected_candidate = {
        "build_id": "candidate",
        "launchd_label": "com.example.webui",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    snapshot_sha256 = "c" * 64
    journal_path = tmp_path / f"early-{failure_point}.json"
    cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=expected_candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "a" * 64,
            "state_snapshot_id": "pre-candidate-snapshot",
            "state_snapshot_sha256": snapshot_sha256,
        },
    )
    for phase, receipt in (
        ("staged", {"generation": 1}),
        ("plist_installed", {"plist_sha256": "b" * 64}),
    ):
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipt,
        )
    if failure_point in {"old_fenced", "old_committed", "rollback_started"}:
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase="old_fenced",
            receipt={"identity": old_identity},
        )
    if failure_point in {"old_committed", "rollback_started"}:
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase="old_committed",
            receipt={"identity": old_identity},
        )
    if failure_point == "rollback_started":
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase="rollback_started",
            receipt={
                "old_identity": old_identity,
                "failed_after_activation": False,
                "old_process_exited": False,
                "error_type": "InjectedCrash",
            },
        )

    admission_state = {
        "value": (
            "committing"
            if failure_point in {"old_committed", "rollback_started"}
            else "fenced"
            if failure_point == "old_fenced"
            else "open"
        )
    }
    injected = set()
    actions = []
    counters = {
        "selector": 0,
        "plist": 0,
        "stop": 0,
        "snapshot": 0,
        "restart": 0,
        "verify": 0,
    }
    drained_activity = {
        "active_streams": 0,
        "active_async_delegations": 0,
        "async_delegations_available": True,
        "active_background_memory_commits": 0,
        "in_flight_memory_commits": 0,
        "memory_commit_activity_available": True,
        "pending_oauth_flows": 0,
        "oauth_activity_available": True,
        "active_terminals": 0,
        "terminal_activity_available": True,
        "running_processes": 0,
        "finalizing_processes": 0,
        "durable_undelivered_completions": 0,
        "process_completion_activity_available": True,
    }

    def inspect_control():
        actions.append("inspect")
        if failure_point == "inspect" and "inspect" not in injected:
            injected.add("inspect")
            raise RuntimeError("injected-inspect-failure")
        return {
            "status": "inspected",
            "transaction_id": transaction_id,
            "identity": old_identity,
            "admission": {
                "state": admission_state["value"],
                "reservations": 0,
                "active_runs": 0,
            },
            "activity": drained_activity,
        }

    def send_control(action, expected, token=None):
        actions.append(action)
        assert expected == old_identity
        if action == "fence":
            admission_state["value"] = "fenced"
            if failure_point == "fence" and "fence" not in injected:
                injected.add("fence")
                raise RuntimeError("injected-fence-response-loss")
            return {
                "status": "fenced",
                "transaction_id": transaction_id,
                "identity": old_identity,
                "fence_token": "recovered-token",
                "admission": {"state": "fenced"},
            }
        if action == "commit":
            if failure_point == "old_fenced" and "commit" not in injected:
                injected.add("commit")
                raise RuntimeError("injected-after-old-fenced")
            admission_state["value"] = "committing"
            return {
                "status": "committing",
                "transaction_id": transaction_id,
                "identity": old_identity,
                "admission": {"state": "committing"},
            }
        assert action == "abort"
        assert token == "recovered-token"
        admission_state["value"] = "open"
        return {
            "status": "aborted",
            "transaction_id": transaction_id,
            "identity": old_identity,
            "admission": {"state": "open"},
        }

    def attest_selector_state():
        if failure_point == "attestation" and "attestation" not in injected:
            injected.add("attestation")
            raise RuntimeError("injected-attestation-failure")
        return {
            "status": "verified",
            "transaction_id": transaction_id,
            "current": "last-good",
            "candidate": "candidate",
            "pending_transaction_id": transaction_id,
        }

    def activate_selection():
        if failure_point == "old_committed":
            raise RuntimeError("injected-after-old-committed")
        raise AssertionError("early failure unexpectedly reached activation")

    def verify_rollback():
        counters["verify"] += 1
        assert admission_state["value"] == "open"
        return {
            "status": "verified",
            "state_snapshot_id": "pre-candidate-snapshot",
            "state_snapshot_sha256": snapshot_sha256,
        }

    initial = None if failure_point == "inspect" else {
        "status": "inspected",
        "transaction_id": transaction_id,
        "identity": old_identity,
        "admission": {"state": admission_state["value"]},
    }
    with pytest.raises(Exception):
        cutover.run_release_control_cutover(
            initial_inspection=initial,
            inspect_control=inspect_control,
            send_control=send_control,
            attest_selector_state=attest_selector_state,
            attest_installed_plist=lambda: {
                "status": "verified",
                "launchd_label": "com.example.webui",
                "plist_sha256": "b" * 64,
            },
            activate_selection=activate_selection,
            promote_selection=lambda: None,
            rollback_selection=lambda: counters.__setitem__(
                "selector", counters["selector"] + 1
            )
            or {"current": "last-good"},
            restore_plist=lambda: counters.__setitem__(
                "plist", counters["plist"] + 1
            )
            or {"plist_sha256": "a" * 64},
            stop_failed_candidate=lambda: counters.__setitem__(
                "stop", counters["stop"] + 1
            ),
            restore_state_snapshot=lambda: counters.__setitem__(
                "snapshot", counters["snapshot"] + 1
            ),
            restart_selection=lambda: counters.__setitem__(
                "restart", counters["restart"] + 1
            ),
            verify_rollback=verify_rollback,
            signal_process=lambda _identity: None,
            wait_for_process_exit=lambda _identity, _timeout: None,
            inspect_candidate_binding=lambda _identity: {},
            inspect_accepted_binding=lambda _identity: {},
            expected_candidate_identity=expected_candidate,
            transaction_id=transaction_id,
            transaction_journal_path=journal_path,
            timeout_seconds=2,
            interval_seconds=0.01,
        )

    assert counters == {
        "selector": 1,
        "plist": 1,
        "stop": 0,
        "snapshot": 0,
        "restart": 0,
        "verify": 1,
    }
    journal = cutover.read_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
    )
    assert {
        "rollback_started",
        "state_rolled_back",
        "plist_restored",
        "failed_candidate_stopped",
        "state_snapshot_restored",
        "last_good_restarted",
        "rollback_verified",
    }.issubset(journal["phases"])
    assert journal["phases"]["state_snapshot_restored"] == {
        "status": "not-required",
        "reason": "candidate_never_accepted",
        "state_snapshot_id": "pre-candidate-snapshot",
        "state_snapshot_sha256": snapshot_sha256,
    }
    if failure_point in {"fence", "old_fenced", "old_committed", "rollback_started"}:
        assert actions[-3:] == ["fence", "abort", "inspect"]
    else:
        assert "abort" not in actions


def _candidate_gateway_precommit_receipts(
    *,
    candidate_identity: dict,
    state_snapshot_id: str,
    state_snapshot_sha256: str,
) -> tuple[tuple[str, dict], ...]:
    last_good_binding = {
        "status": "verified",
        "listener_pid": 41,
        "launchd_pid": 41,
        "pid_start_token": "gateway-old-start",
        "runtime": {"command": "managed-last-good-gateway"},
    }
    candidate_binding = {
        "status": "verified",
        "listener_pid": 42,
        "launchd_pid": 42,
        "pid_start_token": "gateway-candidate-start",
        "runtime": {"command": "managed-candidate-gateway"},
        "admission": "rejecting_new_work",
    }
    return (
        ("gateway_last_good_attested", {"binding": last_good_binding}),
        (
            "watchdog_cron_disable_intent",
            {"prepared": {"status": "prepared"}},
        ),
        ("watchdog_cron_disabled", {"status": "disabled"}),
        ("watchdog_state_reconciled", {"status": "reconciled"}),
        ("gateway_drain_intent", {"status": "drained"}),
        ("gateway_drained", {"status": "drained"}),
        ("gateway_stop_intent", {"status": "planned"}),
        ("gateway_gracefully_stopped", {"status": "stopped"}),
        ("gateway_dispatcher_lock_acquired", {"status": "locked"}),
        ("gateway_workers_quiescent", {"status": "quiescent"}),
        (
            "paired_state_snapshot_created",
            {
                "status": "created",
                "state_snapshot_id": state_snapshot_id,
                "state_snapshot_sha256": state_snapshot_sha256,
            },
        ),
        ("gateway_dispatcher_lock_released", {"status": "released"}),
        (
            "candidate_gateway_start_intent",
            {
                "candidate_build_id": candidate_identity["build_id"],
                "last_good_binding": last_good_binding,
            },
        ),
        ("candidate_gateway_accepted", {"binding": candidate_binding}),
    )


def test_release_rollback_reentry_accepts_already_applied_external_state(tmp_path):
    transaction_id = "rollback-reentry-transaction-000001"
    old_identity = {"pid": 101, "pid_start_token": "old-start"}
    expected_candidate = {
        "build_id": "candidate",
        "launchd_label": "com.example.webui",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    candidate_identity = {
        **expected_candidate,
        "pid": 202,
        "pid_start_token": "candidate-start",
    }
    snapshot_sha256 = "e" * 64
    journal_path = tmp_path / "rollback-reentry.json"
    cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=expected_candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "a" * 64,
            "state_snapshot_id": "pre-candidate-snapshot",
            "state_snapshot_sha256": snapshot_sha256,
        },
    )
    forward_phases = [
        ("staged", {"generation": 1}),
        ("plist_installed", {"plist_sha256": "b" * 64}),
        *_candidate_gateway_precommit_receipts(
            candidate_identity=candidate_identity,
            state_snapshot_id="pre-candidate-snapshot",
            state_snapshot_sha256=snapshot_sha256,
        ),
        ("old_fenced", {"identity": old_identity}),
        ("old_committed", {"identity": old_identity}),
        ("selection_activated", {"selection": {"generation": 2}}),
        ("old_stopped", {"identity": old_identity}),
        ("replacement_proved", {"identity": candidate_identity}),
        (
            "candidate_fenced_health_proved",
            {
                "identity": candidate_identity,
                "admission": {"state": "startup-fenced"},
            },
        ),
    ]
    for phase, receipt in forward_phases:
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipt,
        )
    cutover.record_transaction_phase(
        journal_path,
        transaction_id=transaction_id,
        phase="rollback_started",
        receipt={
            "old_identity": old_identity,
            "failed_after_activation": True,
            "old_process_exited": True,
            "error_type": "InjectedCrash",
        },
    )

    calls = []
    with pytest.raises(cutover.ReleaseBuildError, match="resuming durable rollback"):
        cutover.run_release_control_cutover(
            initial_inspection=None,
            inspect_control=lambda: {
                "status": "inspected",
                "transaction_id": transaction_id,
                "identity": candidate_identity,
                "admission": {"state": "startup-fenced"},
            },
            send_control=lambda *_args: (_ for _ in ()).throw(
                AssertionError("stopped old process must not be reopened")
            ),
            attest_selector_state=lambda: {
                "status": "verified",
                "transaction_id": transaction_id,
                "current": "last-good",
                "candidate": None,
                "pending_transaction_id": None,
            },
            attest_installed_plist=lambda: {
                "status": "verified",
                "launchd_label": "com.example.webui",
                "plist_sha256": "a" * 64,
            },
            activate_selection=lambda: None,
            promote_selection=lambda: None,
            rollback_selection=lambda: calls.append("selector") or {"generation": 3},
            restore_plist=lambda: calls.append("plist") or {"plist_sha256": "a" * 64},
            stop_failed_candidate=lambda: calls.append("stop") or {"pid": 202},
            restore_state_snapshot=lambda: calls.append("snapshot") or {
                "status": "restored",
                "state_snapshot_id": "pre-candidate-snapshot",
                "state_snapshot_sha256": snapshot_sha256,
            },
            restart_selection=lambda: calls.append("restart") or {"pid": 303},
            verify_rollback=lambda: calls.append("verify") or {
                "status": "verified",
                "state_snapshot_id": "pre-candidate-snapshot",
                "state_snapshot_sha256": snapshot_sha256,
            },
            signal_process=lambda _identity: None,
            wait_for_process_exit=lambda _identity, _timeout: None,
            inspect_candidate_binding=lambda _identity: {},
            inspect_accepted_binding=lambda _identity: {},
            expected_candidate_identity=expected_candidate,
            transaction_id=transaction_id,
            transaction_journal_path=journal_path,
            timeout_seconds=2,
            interval_seconds=0.01,
        )

    assert calls == ["selector", "plist", "stop", "snapshot", "restart", "verify"]


@pytest.mark.parametrize(
    "resume_phase",
    [
        "rollback_started",
        "state_rolled_back",
        "plist_restored",
        "failed_candidate_stopped",
        "state_snapshot_restored",
        "last_good_restarted",
        "rollback_verified",
    ],
)
def test_release_transaction_resumes_rollback_and_restores_preaccept_state(
    tmp_path,
    resume_phase,
):
    transaction_id = "rollback-resume-transaction-000001"
    expected_candidate = {
        "build_id": "candidate",
        "launchd_label": "com.example.webui",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    candidate_identity = {
        **expected_candidate,
        "pid": 303,
        "pid_start_token": "candidate-start",
    }
    journal_path = tmp_path / f"{resume_phase}.json"
    snapshot_path = tmp_path / "state.snapshot"
    state_path = tmp_path / "state.db"
    snapshot_path.write_text("preaccept-state", encoding="utf-8")
    state_path.write_text("candidate-mutation", encoding="utf-8")
    cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=expected_candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "a" * 64,
            "state_snapshot_id": "snapshot-before-candidate",
            "state_snapshot_sha256": _sha(snapshot_path),
        },
    )
    old_identity = {"pid": 101, "pid_start_token": "old-start"}
    forward_phases = [
        ("staged", {"generation": 1}),
        ("plist_installed", {"plist_sha256": "b" * 64}),
        *_candidate_gateway_precommit_receipts(
            candidate_identity=candidate_identity,
            state_snapshot_id="snapshot-before-candidate",
            state_snapshot_sha256=_sha(snapshot_path),
        ),
        ("old_fenced", {"identity": old_identity}),
        ("old_committed", {"identity": old_identity}),
        ("selection_activated", {"selection": {"generation": 2}}),
        ("old_stopped", {"identity": old_identity}),
        ("replacement_proved", {"identity": candidate_identity}),
        (
            "candidate_fenced_health_proved",
            {
                "identity": candidate_identity,
                "admission": {"state": "startup-fenced"},
            },
        ),
    ]
    for phase, receipt in forward_phases:
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipt,
        )
    rollback_order = [
        "rollback_started",
        "state_rolled_back",
        "plist_restored",
        "failed_candidate_stopped",
        "state_snapshot_restored",
        "last_good_restarted",
        "rollback_verified",
    ]
    for phase in rollback_order:
        if phase == "state_snapshot_restored":
            receipt = {
                "status": "restored",
                "state_snapshot_id": "snapshot-before-candidate",
                "state_snapshot_sha256": _sha(snapshot_path),
            }
        elif phase == "rollback_verified":
            receipt = {
                "status": "verified",
                "state_snapshot_id": "snapshot-before-candidate",
                "state_snapshot_sha256": _sha(snapshot_path),
            }
        else:
            receipt = {"status": phase}
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipt,
        )
        if phase == "state_snapshot_restored":
            state_path.write_text(snapshot_path.read_text(encoding="utf-8"), encoding="utf-8")
        if phase == resume_phase:
            break

    counters = {
        "state": 0,
        "plist": 0,
        "stop": 0,
        "snapshot": 0,
        "restart": 0,
        "verify": 0,
    }

    def restore_snapshot():
        counters["snapshot"] += 1
        state_path.write_text(snapshot_path.read_text(encoding="utf-8"), encoding="utf-8")
        return {
            "status": "restored",
            "state_snapshot_id": "snapshot-before-candidate",
            "state_snapshot_sha256": _sha(snapshot_path),
        }

    def verify_last_good():
        counters["verify"] += 1
        assert state_path.read_text(encoding="utf-8") == "preaccept-state"
        return {
            "status": "verified",
            "build_id": "last-good",
            "state_snapshot_id": "snapshot-before-candidate",
            "state_snapshot_sha256": _sha(snapshot_path),
        }

    state_was_rolled_back = rollback_order.index(resume_phase) >= rollback_order.index(
        "state_rolled_back"
    )
    plist_was_restored = rollback_order.index(resume_phase) >= rollback_order.index(
        "plist_restored"
    )

    with pytest.raises(cutover.ReleaseBuildError, match="resuming durable rollback"):
        cutover.run_release_control_cutover(
            initial_inspection={
                "status": "inspected",
                "transaction_id": transaction_id,
                "identity": candidate_identity,
                "admission": {"state": "startup-fenced"},
            },
            inspect_control=lambda: (_ for _ in ()).throw(
                AssertionError("rollback resume must not inspect candidate")
            ),
            send_control=lambda *_args: (_ for _ in ()).throw(
                AssertionError("rollback resume must not send release actions")
            ),
            attest_selector_state=lambda: {
                "status": "verified",
                "transaction_id": transaction_id,
                "current": "last-good" if state_was_rolled_back else "candidate",
                "candidate": None if state_was_rolled_back else "candidate",
                "pending_transaction_id": (
                    None if state_was_rolled_back else transaction_id
                ),
            },
            attest_installed_plist=lambda: {
                "status": "verified",
                "launchd_label": "com.example.webui",
                "plist_sha256": ("a" if plist_was_restored else "b") * 64,
            },
            activate_selection=lambda: None,
            promote_selection=lambda: None,
            rollback_selection=lambda: counters.__setitem__(
                "state", counters["state"] + 1
            )
            or {"generation": 3},
            restore_plist=lambda: counters.__setitem__(
                "plist", counters["plist"] + 1
            )
            or {"plist_sha256": "a" * 64},
            stop_failed_candidate=lambda: counters.__setitem__(
                "stop", counters["stop"] + 1
            )
            or {"pid": 303},
            restore_state_snapshot=restore_snapshot,
            restart_selection=lambda: counters.__setitem__(
                "restart", counters["restart"] + 1
            )
            or {"pid": 404},
            verify_rollback=verify_last_good,
            signal_process=lambda _identity: None,
            wait_for_process_exit=lambda _identity, _timeout: None,
            inspect_candidate_binding=lambda _identity: {},
            inspect_accepted_binding=lambda _identity: {},
            expected_candidate_identity=expected_candidate,
            transaction_id=transaction_id,
            transaction_journal_path=journal_path,
            timeout_seconds=2,
            interval_seconds=0.01,
        )

    resume_index = rollback_order.index(resume_phase)
    for index, key in enumerate(
        ("state", "plist", "stop", "snapshot", "restart", "verify"),
        start=1,
    ):
        assert counters[key] == int(resume_index < index)
    assert state_path.read_text(encoding="utf-8") == "preaccept-state"
    final = cutover.read_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
    )
    assert "rollback_verified" in final["phases"]


def test_release_control_driver_aborts_on_process_identity_change(tmp_path):
    transaction_id = "abort-transaction-0000000000000001"
    identity = {
        "pid": 123,
        "pid_start_token": "old-start",
        "instance_id": "instance-a",
    }
    changed = {**identity, "pid": 999}
    expected_candidate = {
        "build_id": "candidate",
        "launchd_label": "com.example.webui",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    journal_path = tmp_path / "abort-transaction.json"
    cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=expected_candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "c" * 64,
            "state_snapshot_id": "snapshot-before-candidate",
            "state_snapshot_sha256": "d" * 64,
        },
    )
    for phase, receipt in (
        ("staged", {"generation": 1}),
        ("plist_installed", {"plist_sha256": "a" * 64}),
    ):
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipt,
        )
    inspection_rows = iter(
        [
            {
                "status": "inspected",
                "transaction_id": transaction_id,
                "identity": changed,
                "admission": {
                    "state": "fenced",
                    "reservations": 0,
                },
            },
            {
                "status": "inspected",
                "transaction_id": transaction_id,
                "identity": identity,
                "admission": {"state": "fenced"},
            },
            {
                "status": "inspected",
                "transaction_id": transaction_id,
                "identity": identity,
                "admission": {"state": "open"},
            },
        ]
    )
    actions = []

    def send_control(action, expected, fence_token=None):
        actions.append(action)
        if action == "fence":
            return {
                "status": "fenced",
                "transaction_id": transaction_id,
                "fence_token": "secret",
                "identity": identity,
            }
        return {
            "status": "aborted",
            "transaction_id": transaction_id,
            "identity": identity,
        }

    with pytest.raises(cutover.DrainIdentityMismatch):
        cutover.run_release_control_cutover(
            initial_inspection={
                "status": "inspected",
                "transaction_id": transaction_id,
                "identity": identity,
            },
            inspect_control=lambda: next(inspection_rows),
            send_control=send_control,
            attest_selector_state=lambda: {
                "status": "verified",
                "transaction_id": transaction_id,
                "current": "last-good",
                "candidate": "candidate",
                "pending_transaction_id": transaction_id,
            },
            attest_installed_plist=lambda: {
                "status": "verified",
                "launchd_label": "com.example.webui",
                "plist_sha256": "a" * 64,
            },
            activate_selection=lambda: None,
            promote_selection=lambda: None,
            rollback_selection=lambda: None,
            restore_plist=lambda: None,
            stop_failed_candidate=lambda: None,
            restore_state_snapshot=lambda: None,
            restart_selection=lambda: None,
            verify_rollback=lambda: {
                "status": "verified",
                "state_snapshot_id": "snapshot-before-candidate",
                "state_snapshot_sha256": "d" * 64,
            },
            signal_process=lambda _identity: None,
            wait_for_process_exit=lambda _identity, _timeout: None,
            inspect_candidate_binding=lambda _identity: {"status": "verified"},
            inspect_accepted_binding=lambda _identity: {"status": "verified"},
            expected_candidate_identity=expected_candidate,
            transaction_id=transaction_id,
            transaction_journal_path=journal_path,
            timeout_seconds=2,
            interval_seconds=0.01,
        )

    assert actions == ["fence", "fence", "abort"]


def test_release_control_driver_surfaces_every_rollback_failure(tmp_path):
    transaction_id = "rollback-transaction-0000000000001"
    identity = {"pid": 123, "pid_start_token": "start-a", "instance_id": "i"}
    inspection = {
        "status": "inspected",
        "transaction_id": transaction_id,
        "identity": identity,
        "admission": {"state": "fenced", "reservations": 0, "active_runs": 0},
        "activity": {
            "active_streams": 0,
            "active_async_delegations": 0,
            "async_delegations_available": True,
            "active_background_memory_commits": 0,
            "in_flight_memory_commits": 0,
            "memory_commit_activity_available": True,
            "pending_oauth_flows": 0,
            "oauth_activity_available": True,
            "active_terminals": 0,
            "terminal_activity_available": True,
            "running_processes": 0,
            "finalizing_processes": 0,
            "durable_undelivered_completions": 0,
            "process_completion_activity_available": True,
        },
    }
    expected_candidate = {
        "build_id": "candidate",
        "launchd_label": "com.example.webui",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    journal_path = tmp_path / "rollback-transaction.json"
    cutover.initialize_transaction_journal(
        journal_path,
        transaction_id=transaction_id,
        expected_candidate_identity=expected_candidate,
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "c" * 64,
            "state_snapshot_id": "snapshot-before-candidate",
            "state_snapshot_sha256": "d" * 64,
        },
    )
    for phase, receipt in (
        ("staged", {"generation": 1}),
        ("plist_installed", {"plist_sha256": "b" * 64}),
    ):
        cutover.record_transaction_phase(
            journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipt,
        )
    actions = []
    admission_state = {"value": "open"}

    def send_control(action, _expected, _token=None):
        actions.append(action)
        if action == "fence":
            admission_state["value"] = "fenced"
            return {
                "status": "fenced",
                "transaction_id": transaction_id,
                "fence_token": "token",
                "identity": identity,
            }
        if action == "commit":
            admission_state["value"] = "committing"
            return {
                "status": "committing",
                "transaction_id": transaction_id,
                "identity": identity,
            }
        admission_state["value"] = "open"
        return {
            "status": "aborted",
            "transaction_id": transaction_id,
            "identity": identity,
        }

    with pytest.raises(cutover.ReleaseBuildError) as failure:
        cutover.run_release_control_cutover(
            initial_inspection={
                "status": "inspected",
                "transaction_id": transaction_id,
                "identity": identity,
            },
            inspect_control=lambda: {
                **inspection,
                "admission": {
                    **inspection["admission"],
                    "state": admission_state["value"],
                },
            },
            send_control=send_control,
            attest_selector_state=lambda: {
                "status": "verified",
                "transaction_id": transaction_id,
                "current": "last-good",
                "candidate": "candidate",
                "pending_transaction_id": transaction_id,
            },
            attest_installed_plist=lambda: {
                "status": "verified",
                "launchd_label": "com.example.webui",
                "plist_sha256": "b" * 64,
            },
            activate_selection=lambda: actions.append("activate"),
            promote_selection=lambda: actions.append("promote"),
            rollback_selection=lambda: (_ for _ in ()).throw(
                RuntimeError("rollback-state-error")
            ),
            restore_plist=lambda: (_ for _ in ()).throw(
                RuntimeError("restore-plist-error")
            ),
            stop_failed_candidate=lambda: (_ for _ in ()).throw(
                RuntimeError("stop-candidate-error")
            ),
            restore_state_snapshot=lambda: (_ for _ in ()).throw(
                RuntimeError("restore-state-error")
            ),
            restart_selection=lambda: (_ for _ in ()).throw(
                RuntimeError("restart-label-error")
            ),
            verify_rollback=lambda: (_ for _ in ()).throw(
                RuntimeError("rollback-proof-error")
            ),
            signal_process=lambda _identity: (_ for _ in ()).throw(
                cutover.DrainIdentityMismatch("signal identity changed")
            ),
            wait_for_process_exit=lambda _identity, _timeout: None,
            inspect_candidate_binding=lambda _identity: {"status": "verified"},
            inspect_accepted_binding=lambda _identity: {"status": "verified"},
            expected_candidate_identity=expected_candidate,
            transaction_id=transaction_id,
            transaction_journal_path=journal_path,
            timeout_seconds=2,
            interval_seconds=0.01,
        )

    message = str(failure.value)
    assert "signal identity changed" in message
    assert "rollback-state-error" in message
    assert "restore-plist-error" in message
    assert "stop-candidate-error" in message
    assert "restore-state-error" not in message
    assert "restart-label-error" not in message
    assert "rollback-proof-error" in message
    assert actions == ["fence", "commit", "activate", "fence", "abort"]


def test_release_control_receipt_rejects_forged_attestation():
    key = b"r" * 32
    receipt = {
        "status": "inspected",
        "transaction_id": "t" * 32,
        "request_nonce": "n" * 32,
        "identity": {"pid": 123},
        "attestation": "0" * 64,
    }

    with pytest.raises(cutover.ReleaseBuildError, match="attestation"):
        cutover._verify_release_control_receipt(
            receipt,
            signing_key=key,
            transaction_id="t" * 32,
            request_nonce="n" * 32,
        )


def test_exact_process_signal_refuses_pid_reuse(monkeypatch):
    killed = []
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "new-process")
    monkeypatch.setattr(cutover.os, "kill", lambda *args: killed.append(args))

    with pytest.raises(cutover.DrainIdentityMismatch, match="identity changed"):
        cutover.signal_exact_release_process(
            {"pid": 123, "pid_start_token": "old-process"}
        )
    assert killed == []


def test_state_snapshot_adopts_manifest_gap_and_restores_exact_directory(tmp_path):
    state_dir = tmp_path / "mutable" / "state"
    state_dir.mkdir(parents=True)
    _write(state_dir / "state.db", "before-db")
    _write(state_dir / "state.db-wal", "before-wal")
    snapshot_root = tmp_path / "control" / "snapshot"
    manifest_path = tmp_path / "control" / "snapshot.json"
    snapshot_id = "snapshot-transaction-00000000000001"

    created = cutover.create_state_snapshot(
        [str(state_dir)],
        snapshot_root=snapshot_root,
        manifest_path=manifest_path,
        snapshot_id=snapshot_id,
    )
    original_manifest = manifest_path.read_bytes()
    manifest_path.unlink()

    adopted = cutover.create_state_snapshot(
        [str(state_dir)],
        snapshot_root=snapshot_root,
        manifest_path=manifest_path,
        snapshot_id=snapshot_id,
    )

    assert manifest_path.read_bytes() == original_manifest
    assert adopted["state_snapshot_sha256"] == created["state_snapshot_sha256"]
    _write(state_dir / "state.db", "candidate-db")
    _write(state_dir / "candidate-created.registry", "must disappear")
    (state_dir / "state.db-wal").unlink()

    restored = cutover.restore_state_snapshot_from_manifest(
        manifest_path,
        expected_snapshot_id=snapshot_id,
        expected_manifest_sha256=created["state_snapshot_sha256"],
    )

    assert restored == {
        "status": "restored",
        "state_snapshot_id": snapshot_id,
        "state_snapshot_sha256": created["state_snapshot_sha256"],
    }
    assert (state_dir / "state.db").read_text() == "before-db"
    assert (state_dir / "state.db-wal").read_text() == "before-wal"
    assert not (state_dir / "candidate-created.registry").exists()


def test_state_snapshot_tombstone_removes_candidate_created_optional_target(tmp_path):
    mutable_parent = tmp_path / "mutable"
    mutable_parent.mkdir()
    optional_target = mutable_parent / "optional-registry.json"
    snapshot_root = tmp_path / "control" / "snapshot"
    manifest_path = tmp_path / "control" / "snapshot.json"
    snapshot_id = "snapshot-tombstone-transaction-000001"

    created = cutover.create_state_snapshot(
        [str(optional_target)],
        snapshot_root=snapshot_root,
        manifest_path=manifest_path,
        snapshot_id=snapshot_id,
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["metadata_contract"] == "path-kind-content-mode"
    assert manifest["entries"][0]["kind"] == "absent"

    _write(optional_target, "candidate-created\n")
    restored = cutover.restore_state_snapshot_from_manifest(
        manifest_path,
        expected_snapshot_id=snapshot_id,
        expected_manifest_sha256=created["state_snapshot_sha256"],
    )

    assert restored["status"] == "restored"
    assert not optional_target.exists()


@pytest.mark.parametrize("crash_point", ["after-backup", "after-publish"])
def test_directory_snapshot_restore_recovers_atomic_replace_crash(
    tmp_path, monkeypatch, crash_point
):
    state_dir = tmp_path / "mutable" / "state"
    state_dir.mkdir(parents=True)
    _write(state_dir / "state.db", "before\n")
    _chmod(state_dir / "state.db", 0o640)
    snapshot_root = tmp_path / "control" / "snapshot"
    manifest_path = tmp_path / "control" / "snapshot.json"
    snapshot_id = f"snapshot-restore-{crash_point}-000000001"
    created = cutover.create_state_snapshot(
        [str(state_dir)],
        snapshot_root=snapshot_root,
        manifest_path=manifest_path,
        snapshot_id=snapshot_id,
    )
    _write(state_dir / "state.db", "candidate\n")
    _write(state_dir / "candidate-only", "remove me\n")

    stage = state_dir.parent / (
        f".{state_dir.name}.hermes-restore-{snapshot_id}-0000.stage"
    )
    backup = state_dir.parent / (
        f".{state_dir.name}.hermes-restore-{snapshot_id}-0000.replaced"
    )
    real_replace = cutover.os.replace
    crashed = False

    def crash_after_replace(source, destination):
        nonlocal crashed
        source_path = Path(source)
        destination_path = Path(destination)
        should_crash = (
            crash_point == "after-backup"
            and source_path == state_dir
            and destination_path == backup
        ) or (
            crash_point == "after-publish"
            and source_path == stage
            and destination_path == state_dir
        )
        real_replace(source, destination)
        if should_crash and not crashed:
            crashed = True
            raise cutover.InjectedCutoverCrash(crash_point)

    monkeypatch.setattr(cutover.os, "replace", crash_after_replace)
    with pytest.raises(cutover.InjectedCutoverCrash, match=crash_point):
        cutover.restore_state_snapshot_from_manifest(
            manifest_path,
            expected_snapshot_id=snapshot_id,
            expected_manifest_sha256=created["state_snapshot_sha256"],
        )
    monkeypatch.setattr(cutover.os, "replace", real_replace)

    restored = cutover.restore_state_snapshot_from_manifest(
        manifest_path,
        expected_snapshot_id=snapshot_id,
        expected_manifest_sha256=created["state_snapshot_sha256"],
    )

    assert restored["status"] == "restored"
    assert (state_dir / "state.db").read_text() == "before\n"
    assert stat.S_IMODE((state_dir / "state.db").stat().st_mode) == 0o640
    assert not (state_dir / "candidate-only").exists()
    assert not stage.exists()
    assert not backup.exists()


def test_atomic_copy_streams_without_path_read_bytes(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    source.write_bytes(b"state" * 1024 * 1024)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path):
        if path == source:
            raise AssertionError("large mutable files must not be read into memory")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    receipt = cutover._atomic_copy_file(source, destination)

    assert receipt["sha256"] == _sha(destination)
    assert destination.stat().st_size == source.stat().st_size


def test_wait_for_exact_process_exit_does_not_treat_ps_failure_as_exit(monkeypatch):
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(cutover.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "S")

    with pytest.raises(cutover.DrainIdentityMismatch, match="probe failed"):
        cutover.wait_for_exact_process_exit(
            {"pid": 123, "pid_start_token": "same-live-process"},
            0.2,
        )


def test_wait_for_exact_process_exit_accepts_authorized_terminal_zombie(
    monkeypatch,
):
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(cutover.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "Z")

    cutover.wait_for_exact_process_exit(
        {"pid": 123, "pid_start_token": "same-signaled-process"},
        0.2,
        allow_exact_signaled_zombie=True,
    )


def test_wait_for_exact_process_exit_accepts_transient_post_sigkill_state(
    monkeypatch,
):
    states = iter(("T", "Z"))
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(cutover.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: next(states))
    monkeypatch.setattr(cutover.time, "sleep", lambda _seconds: None)

    cutover.wait_for_exact_process_exit(
        {"pid": 123, "pid_start_token": "same-signaled-process"},
        0.2,
        allow_exact_signaled_zombie=True,
    )


def test_wait_for_exact_process_exit_bounds_transient_post_sigkill_state(
    monkeypatch,
):
    clock = iter((0.0, 0.0, 0.2))
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(cutover.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "S")
    monkeypatch.setattr(cutover.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cutover.time, "sleep", lambda _seconds: None)

    with pytest.raises(
        cutover.DrainTimeout,
        match="committed release process did not exit",
    ):
        cutover.wait_for_exact_process_exit(
            {"pid": 123, "pid_start_token": "same-signaled-process"},
            0.1,
            allow_exact_signaled_zombie=True,
        )


def test_wait_for_exact_process_exit_rejects_unauthorized_zombie(monkeypatch):
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(cutover.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "Z")

    with pytest.raises(cutover.DrainIdentityMismatch, match="probe failed"):
        cutover.wait_for_exact_process_exit(
            {"pid": 123, "pid_start_token": "unproved-process"},
            0.2,
        )


def test_stop_current_service_allows_exact_signaled_terminal_zombie(monkeypatch):
    plan = {
        "listener_port": 8787,
        "timeout_seconds": 1,
    }
    runtime = {
        "pid": 41,
        "pid_start_token": "candidate-start",
        "program_identity": {"sha256": "a" * 64},
    }
    listener_probes = iter((41, None))
    job_probes = iter((41, None))
    signals = []

    def listener(_port):
        value = next(listener_probes)
        if value is None:
            raise cutover.DrainIdentityMismatch("listener absent")
        return value

    monkeypatch.setattr(cutover, "_listener_pid", listener)
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: next(job_probes),
    )
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "candidate-start")
    monkeypatch.setattr(
        cutover,
        "_listener_process_receipt",
        lambda *_args, **_kwargs: runtime,
    )
    monkeypatch.setattr(
        cutover,
        "_runtime_receipt_matches",
        lambda actual, expected: actual == expected,
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *_args, **_kwargs: {"status": "stopped"},
    )
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: True)
    monkeypatch.setattr(
        cutover.os,
        "kill",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    def wait(identity, timeout_seconds, *, allow_exact_signaled_zombie):
        assert identity == {
            "pid": 41,
            "pid_start_token": "candidate-start",
        }
        assert timeout_seconds == 1
        assert allow_exact_signaled_zombie is True

    monkeypatch.setattr(cutover, "wait_for_exact_process_exit", wait)

    receipt = cutover._stop_current_service(
        plan,
        gateway=False,
        authorized_receipts=[runtime],
    )

    assert receipt["status"] == "stopped"
    assert signals == [(41, signal.SIGKILL)]


def test_stop_current_service_collects_source_for_source_bound_authorization(
    monkeypatch,
):
    plan = {
        "listener_port": 8787,
        "timeout_seconds": 1,
    }
    runtime = {
        "pid": 41,
        "pid_start_token": "legacy-start",
        "command": "legacy command",
        "source": {"tree": "a" * 40},
        "routing_environment": {"HERMES_WEBUI_PORT": "8787"},
    }
    listener_probes = iter((41, None))
    job_probes = iter((41, None))
    source_requests = []

    def listener(_port):
        value = next(listener_probes)
        if value is None:
            raise cutover.ListenerAbsent("listener absent")
        return value

    monkeypatch.setattr(cutover, "_listener_pid", listener)
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: next(job_probes),
    )
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "legacy-start")

    def process_receipt(_plan, *, gateway, require_git_source):
        source_requests.append(require_git_source)
        return runtime

    monkeypatch.setattr(
        cutover,
        "_listener_process_receipt",
        process_receipt,
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *_args, **_kwargs: {"status": "stopped"},
    )
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: False)

    receipt = cutover._stop_current_service(
        plan,
        gateway=False,
        authorized_receipts=[runtime],
    )

    assert receipt["status"] == "stopped"
    assert source_requests == [True]


def test_wait_for_exact_process_exit_accepts_reap_during_zombie_probe(
    monkeypatch,
):
    probes = iter(("present", "absent"))
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)

    def fake_kill(_pid, sent_signal):
        assert sent_signal == 0
        if next(probes) == "absent":
            raise ProcessLookupError

    monkeypatch.setattr(cutover.os, "kill", fake_kill)
    monkeypatch.setattr(
        cutover,
        "_ps_value",
        lambda _pid, _field: (_ for _ in ()).throw(
            cutover.DrainIdentityMismatch("process identity probe failed")
        ),
    )

    cutover.wait_for_exact_process_exit(
        {"pid": 123, "pid_start_token": "same-signaled-process"},
        0.2,
        allow_exact_signaled_zombie=True,
    )


def test_resume_frozen_writers_tolerates_already_terminal_leaf(monkeypatch):
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 123,
                        "pid_start_token": "terminal-leaf",
                        "ppid": 122,
                        "state": "T",
                    },
                    {
                        "pid": 122,
                        "pid_start_token": "exact-root",
                        "ppid": None,
                        "state": "Ts",
                    },
                ],
            }
        ]
    }
    continued = []

    monkeypatch.setattr(
        cutover,
        "_exact_process_is_alive",
        lambda process: process["pid"] == 122,
    )
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "S")

    def fake_kill(pid, sent_signal):
        if pid == 123 and sent_signal == 0:
            raise ProcessLookupError
        continued.append((pid, sent_signal))

    monkeypatch.setattr(cutover.os, "kill", fake_kill)

    receipt = cutover._resume_frozen_prepared_writers(frozen)

    assert receipt["status"] == "resumed-with-terminal-children"
    assert receipt["processes"] == [
        {"pid": 122, "pid_start_token": "exact-root"}
    ]
    assert receipt["terminal_processes"] == [
        {
            "pid": 123,
            "pid_start_token": "terminal-leaf",
            "status": "absent",
        }
    ]
    assert continued == [(122, signal.SIGCONT)]


def test_resume_frozen_writers_tolerates_terminal_zombie_leaf(monkeypatch):
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 123,
                        "pid_start_token": "terminal-leaf",
                        "ppid": 122,
                        "state": "T",
                    },
                    {
                        "pid": 122,
                        "pid_start_token": "exact-root",
                        "ppid": None,
                        "state": "Ts",
                    },
                ],
            }
        ]
    }
    continued = []
    monkeypatch.setattr(
        cutover,
        "_exact_process_is_alive",
        lambda process: process["pid"] == 122,
    )
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(
        cutover,
        "_ps_value",
        lambda pid, _field: "Z" if pid == 123 else "S",
    )
    monkeypatch.setattr(
        cutover.os,
        "kill",
        lambda pid, sent_signal: continued.append((pid, sent_signal))
        if sent_signal == signal.SIGCONT
        else None,
    )

    receipt = cutover._resume_frozen_prepared_writers(frozen)

    assert receipt["status"] == "resumed-with-terminal-children"
    assert receipt["terminal_processes"] == [
        {
            "pid": 123,
            "pid_start_token": "terminal-leaf",
            "status": "zombie",
        }
    ]
    assert continued == [(122, signal.SIGCONT)]


def test_resume_frozen_writers_tolerates_leaf_reaped_during_state_probe(
    monkeypatch,
):
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 123,
                        "pid_start_token": "terminal-leaf",
                        "ppid": 122,
                        "state": "T",
                    },
                    {
                        "pid": 122,
                        "pid_start_token": "exact-root",
                        "ppid": None,
                        "state": "Ts",
                    },
                ],
            }
        ]
    }
    leaf_probes = iter(("present", "absent"))
    monkeypatch.setattr(
        cutover,
        "_exact_process_is_alive",
        lambda process: process["pid"] == 122,
    )
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)

    def fake_kill(pid, sent_signal):
        if pid == 123 and sent_signal == 0:
            if next(leaf_probes) == "absent":
                raise ProcessLookupError

    monkeypatch.setattr(cutover.os, "kill", fake_kill)
    monkeypatch.setattr(
        cutover,
        "_ps_value",
        lambda pid, _field: (_ for _ in ()).throw(
            cutover.DrainIdentityMismatch("process identity probe failed")
        )
        if pid == 123
        else "S",
    )

    receipt = cutover._resume_frozen_prepared_writers(frozen)

    assert receipt["terminal_processes"] == [
        {
            "pid": 123,
            "pid_start_token": "terminal-leaf",
            "status": "absent",
        }
    ]


def test_resume_frozen_writers_rejects_reused_leaf_pid(monkeypatch):
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 123,
                        "pid_start_token": "terminal-leaf",
                        "ppid": 122,
                        "state": "T",
                    },
                    {
                        "pid": 122,
                        "pid_start_token": "exact-root",
                        "ppid": None,
                        "state": "Ts",
                    },
                ],
            }
        ]
    }
    monkeypatch.setattr(
        cutover,
        "_exact_process_is_alive",
        lambda process: process["pid"] == 122,
    )
    monkeypatch.setattr(
        cutover,
        "_pid_start_token",
        lambda pid: "reused-leaf" if pid == 123 else None,
    )
    monkeypatch.setattr(
        cutover.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(
            AssertionError("no process may be resumed before validation completes")
        ),
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="frozen child PID was reused before SIGCONT",
    ):
        cutover._resume_frozen_prepared_writers(frozen)


def test_resume_frozen_writers_resumes_parent_before_lower_pid_child(monkeypatch):
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 100,
                        "pid_start_token": "child",
                        "ppid": 300,
                        "state": "T",
                    },
                    {
                        "pid": 300,
                        "pid_start_token": "root",
                        "ppid": None,
                        "state": "Ts",
                    },
                ],
            }
        ]
    }
    continued = []
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _process: True)
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "S")
    monkeypatch.setattr(
        cutover.os,
        "kill",
        lambda pid, sent_signal: continued.append((pid, sent_signal)),
    )

    receipt = cutover._resume_frozen_prepared_writers(frozen)

    assert receipt["status"] == "resumed"
    assert continued == [
        (300, signal.SIGCONT),
        (100, signal.SIGCONT),
    ]


def test_restore_frozen_webui_adopts_exact_binding_after_root_retired(
    monkeypatch,
):
    prepared = {
        "legacy": {
            "pid": 300,
            "pid_start_token": "retired-root",
        }
    }
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 300,
                        "pid_start_token": "retired-root",
                        "ppid": None,
                        "state": "Ts",
                    }
                ],
            }
        ]
    }
    binding = {
        "status": "verified",
        "pid": 901,
        "pid_start_token": "restored-root",
    }
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: False)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(
        cutover,
        "_attest_restored_legacy_binding",
        lambda _plan, *, prepared, gateway: binding,
    )
    monkeypatch.setattr(
        cutover,
        "_resume_frozen_prepared_writers",
        lambda _frozen: (_ for _ in ()).throw(
            AssertionError("retired root cannot be resumed")
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_bootstrap_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact restored binding must be adopted")
        ),
    )

    receipt = cutover._restore_or_resume_frozen_legacy_webui(
        {},
        prepared=prepared,
        frozen=frozen,
    )

    assert receipt["binding"] == binding
    assert receipt["writers"] == {
        "status": "adopted-exact-restored-binding",
        "retired_root": {
            "pid": 300,
            "pid_start_token": "retired-root",
        },
        "restored_root": {
            "pid": 901,
            "pid_start_token": "restored-root",
        },
    }


def test_restore_frozen_webui_resumes_exact_live_root(monkeypatch):
    prepared = {
        "legacy": {
            "pid": 300,
            "pid_start_token": "live-root",
        }
    }
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 300,
                        "pid_start_token": "live-root",
                        "ppid": None,
                        "state": "Ts",
                    }
                ],
            }
        ]
    }
    binding = {
        "status": "verified",
        "pid": 300,
        "pid_start_token": "live-root",
    }
    resumed = {"status": "resumed", "processes": [{"pid": 300}]}
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: True)
    monkeypatch.setattr(
        cutover,
        "_resume_frozen_prepared_writers",
        lambda actual: resumed if actual == frozen else None,
    )
    monkeypatch.setattr(
        cutover,
        "_wait_for_legacy_binding",
        lambda _plan, *, prepared, gateway: binding,
    )
    monkeypatch.setattr(
        cutover,
        "_bootstrap_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("live root must be resumed")
        ),
    )

    receipt = cutover._restore_or_resume_frozen_legacy_webui(
        {},
        prepared=prepared,
        frozen=frozen,
    )

    assert receipt == {"writers": resumed, "binding": binding}


def test_restore_frozen_webui_restarts_only_at_proven_absence(monkeypatch):
    prepared = {
        "legacy": {
            "pid": 300,
            "pid_start_token": "retired-root",
        }
    }
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 300,
                        "pid_start_token": "retired-root",
                        "ppid": None,
                        "state": "Ts",
                    }
                ],
            }
        ]
    }
    binding = {
        "status": "verified",
        "pid": 902,
        "pid_start_token": "restarted-root",
    }
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: False)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(
        cutover,
        "_attest_restored_legacy_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cutover.ReleaseBuildError("binding absent")
        ),
    )
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: None)
    monkeypatch.setattr(cutover, "_job_pid", lambda _plan, gateway: None)
    monkeypatch.setattr(
        cutover,
        "_bootstrap_job",
        lambda _plan, plist, *, gateway: {
            "status": "started",
            "plist": plist,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_wait_for_legacy_binding",
        lambda _plan, *, prepared, gateway: binding,
    )

    receipt = cutover._restore_or_resume_frozen_legacy_webui(
        {
            "listener_port": 8787,
            "bootstrap_rollback_plist": "/tmp/rollback.plist",
        },
        prepared=prepared,
        frozen=frozen,
    )

    assert receipt["binding"] == binding
    assert receipt["writers"] == {
        "status": "restarted-after-exact-root-retirement",
        "retired_root": {
            "pid": 300,
            "pid_start_token": "retired-root",
        },
        "restored_root": {
            "pid": 902,
            "pid_start_token": "restarted-root",
        },
        "restart": {
            "status": "started",
            "plist": "/tmp/rollback.plist",
        },
    }


def test_stop_prepared_service_kills_children_before_parent(monkeypatch):
    tree = [
        {
            "pid": 100,
            "pid_start_token": "child",
            "ppid": 300,
            "state": "T",
        },
        {
            "pid": 300,
            "pid_start_token": "root",
            "ppid": None,
            "state": "Ts",
        },
    ]
    signaled = []
    monkeypatch.setattr(cutover, "_job_pid", lambda _plan, gateway: None)
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _process: True)
    monkeypatch.setattr(
        cutover.os,
        "kill",
        lambda pid, sent_signal: signaled.append((pid, sent_signal)),
    )
    monkeypatch.setattr(
        cutover,
        "wait_for_exact_process_exit",
        lambda _process, _timeout, **_kwargs: None,
    )
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: None)

    cutover._stop_prepared_service(
        {"timeout_seconds": 1.0, "listener_port": 8787},
        {"pid": 300, "pid_start_token": "root"},
        gateway=False,
        frozen_tree={"tree": tree},
        bootout_receipt={"status": "stopped"},
    )

    assert signaled == [
        (100, signal.SIGKILL),
        (300, signal.SIGKILL),
    ]


def test_bootout_prepared_jobs_accepts_exact_frozen_root_retirement(
    monkeypatch,
):
    prepared = {
        "legacy": {
            "pid": 300,
            "pid_start_token": "root",
        }
    }
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: None if gateway else 300,
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda _plan, *, gateway, required: {
            "status": "stopped",
            "gateway": gateway,
            "required": required,
        },
    )
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: True)
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "T")

    receipt = cutover._bootout_prepared_jobs({}, prepared)

    assert receipt["status"] == "bootout-requested"
    assert receipt["jobs"]["webui"]["retirement"] == (
        "pending-exact-frozen-root"
    )
    assert receipt["jobs"]["gateway"] == {
        "status": "already-gracefully-stopped"
    }


def test_stop_prepared_service_retires_pending_exact_launchd_job(
    monkeypatch,
):
    root = {
        "pid": 300,
        "pid_start_token": "root",
        "ppid": None,
        "state": "Ts",
    }
    job_probes = 0

    def job_pid(_plan, *, gateway):
        nonlocal job_probes
        assert gateway is False
        job_probes += 1
        return 300 if job_probes <= 2 else None

    signaled = []
    monkeypatch.setattr(cutover, "_job_pid", job_pid)
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: True)
    monkeypatch.setattr(
        cutover.os,
        "kill",
        lambda pid, sent_signal: signaled.append((pid, sent_signal)),
    )
    monkeypatch.setattr(
        cutover,
        "wait_for_exact_process_exit",
        lambda _process, _timeout, **_kwargs: None,
    )
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: None)

    receipt = cutover._stop_prepared_service(
        {
            "timeout_seconds": 1.0,
            "interval_seconds": 0.001,
            "listener_port": 8787,
        },
        {"pid": 300, "pid_start_token": "root"},
        gateway=False,
        frozen_tree={"tree": [root]},
        bootout_receipt={
            "status": "stopped",
            "retirement": "pending-exact-frozen-root",
        },
    )

    assert receipt["status"] == "stopped"
    assert receipt["launchd_retirement"] == "verified-absent"
    assert signaled == [(300, signal.SIGKILL)]
    assert job_probes >= 3


def test_darwin_pid_start_token_distinguishes_same_second_pid_reuse(monkeypatch):
    start_times = iter([(123456, 100), (123456, 900)])

    class FakeProcPidInfo:
        argtypes = None
        restype = None

        def __call__(self, pid, flavor, arg, buffer, buffer_size):
            assert pid == 123
            assert flavor == 3
            assert arg == 0
            assert buffer_size == 136
            seconds, microseconds = next(start_times)
            import struct as struct_module

            struct_module.pack_into("=QQ", buffer, 120, seconds, microseconds)
            return buffer_size

    fake_proc_pidinfo = FakeProcPidInfo()
    fake_libproc = SimpleNamespace(proc_pidinfo=fake_proc_pidinfo)
    monkeypatch.setattr(cutover.sys, "platform", "darwin")
    monkeypatch.setattr(cutover.ctypes, "CDLL", lambda *_args, **_kwargs: fake_libproc)

    first = cutover._pid_start_token(123)
    second = cutover._pid_start_token(123)

    assert first == "darwin-proc:123:123456:100"
    assert second == "darwin-proc:123:123456:900"
    assert first != second


def test_writer_barrier_freezes_root_and_complete_descendant_tree(monkeypatch):
    tokens = {10: "root-start", 11: "child-start", 12: "grandchild-start"}
    table = {10: 1, 11: 10, 12: 11, 99: 1}
    signals = []
    monkeypatch.setattr(cutover, "_process_parent_table", lambda: dict(table))
    monkeypatch.setattr(cutover, "_pid_start_token", lambda pid: tokens.get(pid))
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, field: "T" if field == "state" else "")
    monkeypatch.setattr(
        cutover.os,
        "kill",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    receipt = cutover._freeze_exact_process_tree(
        {"pid": 10, "pid_start_token": "root-start"},
        role="webui",
    )

    assert [row["pid"] for row in receipt["tree"]] == [10, 11, 12]
    assert {pid for pid, _signal in signals} == {10, 11, 12}
    assert all(sent_signal == cutover.signal.SIGSTOP for _pid, sent_signal in signals)


def test_frozen_boundary_accepts_exact_idle_descendant_tree(monkeypatch):
    plan = {"gateway_listener_port": 8642}
    prepared = {
        "legacy": {"pid": 10, "pid_start_token": "root-start"},
        "gateway": {"pid": 20, "pid_start_token": "gateway-start"},
    }
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 9,
                        "ppid": 10,
                        "pid_start_token": "child-start",
                        "state": "T",
                    },
                    {
                        "pid": 10,
                        "ppid": None,
                        "pid_start_token": "root-start",
                        "state": "T",
                    },
                ],
            }
        ]
    }

    monkeypatch.setattr(
        cutover,
        "_verify_frozen_prepared_writers",
        lambda *_args: frozen,
    )
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: None)
    monkeypatch.setattr(cutover, "_job_pid", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cutover,
        "_verify_legacy_dispatcher_lock",
        lambda _plan, receipt: receipt,
    )
    monkeypatch.setattr(
        cutover,
        "_wait_for_legacy_kanban_quiescence",
        lambda _plan: {"status": "verified"},
    )
    monkeypatch.setattr(
        cutover,
        "_established_socket_boundary_receipt",
        lambda *_args, **_kwargs: {"status": "verified"},
    )
    monkeypatch.setattr(
        cutover,
        "_legacy_durable_activity_receipt",
        lambda _plan: {"status": "verified"},
    )
    monkeypatch.setattr(
        cutover,
        "_inspect_synthetic_completion_stores",
        lambda _plan: {"status": "verified"},
    )

    observed = cutover._prove_frozen_legacy_boundary(
        plan,
        prepared,
        frozen,
        {"status": "held"},
    )

    assert observed["processes"]["webui"] == {
        "pid": 10,
        "pid_start_token": "root-start",
        "children": 1,
    }


def test_frozen_boundary_rejects_ambiguous_gateway_listener(monkeypatch):
    plan = {"gateway_listener_port": 8642}
    prepared = {
        "legacy": {"pid": 10, "pid_start_token": "root-start"},
        "gateway": {"pid": 20, "pid_start_token": "gateway-start"},
    }
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 10,
                        "ppid": None,
                        "pid_start_token": "root-start",
                        "state": "T",
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(
        cutover,
        "_verify_frozen_prepared_writers",
        lambda *_args: frozen,
    )
    monkeypatch.setattr(
        cutover,
        "_listener_pid",
        lambda _port: (_ for _ in ()).throw(
            cutover.ListenerProbeAmbiguous("ambiguous listener owners")
        ),
    )

    with pytest.raises(
        cutover.ListenerProbeAmbiguous,
        match="ambiguous listener owners",
    ):
        cutover._prove_frozen_legacy_boundary(
            plan,
            prepared,
            frozen,
            {"status": "held"},
        )


def test_legacy_durable_activity_ignores_expired_webui_rows(
    tmp_path, monkeypatch
):
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            """
            CREATE TABLE session_activity (
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                PRIMARY KEY (session_id, run_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO session_activity VALUES (?, ?, ?, ?, ?, ?)",
            ("sid", "run-stale", "webui", "running", 900.0, 979.0),
        )
    monkeypatch.setattr(cutover.time, "time", lambda: 1000.0)
    monkeypatch.setattr(
        cutover,
        "_legacy_process_checkpoint_receipt",
        lambda _plan: {"status": "verified"},
    )

    receipt = cutover._legacy_durable_activity_receipt(
        {"legacy_state_db": str(state_db)}
    )

    assert receipt["status"] == "verified"
    assert receipt["webui_active_run_leases"] == 0
    assert receipt["expired_webui_activity_rows"] == 1
    assert receipt["activity_ttl_seconds"] == 20.0


def test_legacy_durable_activity_rejects_fresh_webui_row(
    tmp_path, monkeypatch
):
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            """
            CREATE TABLE session_activity (
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                PRIMARY KEY (session_id, run_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO session_activity VALUES (?, ?, ?, ?, ?, ?)",
            ("sid", "run-fresh", "webui-native", "tool", 990.0, 980.0),
        )
    monkeypatch.setattr(cutover.time, "time", lambda: 1000.0)

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="legacy WebUI still has durable active-run leases",
    ):
        cutover._legacy_durable_activity_receipt(
            {"legacy_state_db": str(state_db)}
        )


def test_legacy_durable_activity_rejects_invalid_heartbeat(
    tmp_path, monkeypatch
):
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            """
            CREATE TABLE session_activity (
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                PRIMARY KEY (session_id, run_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO session_activity VALUES (?, ?, ?, ?, ?, ?)",
            ("sid", "run-invalid", "webui", "running", 900.0, "invalid"),
        )
    monkeypatch.setattr(cutover.time, "time", lambda: 1000.0)

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="legacy activity lease heartbeat is invalid",
    ):
        cutover._legacy_durable_activity_receipt(
            {"legacy_state_db": str(state_db)}
        )


def test_legacy_durable_activity_rejects_invalid_ttl(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            """
            CREATE TABLE session_activity (
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                PRIMARY KEY (session_id, run_id)
            )
            """
        )
    monkeypatch.setattr(
        cutover, "SESSION_ACTIVITY_TTL_SECONDS", float("nan")
    )

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="legacy activity lease TTL is invalid",
    ):
        cutover._legacy_durable_activity_receipt(
            {"legacy_state_db": str(state_db)}
        )


def test_legacy_durable_activity_accepts_empty_table(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            """
            CREATE TABLE session_activity (
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                PRIMARY KEY (session_id, run_id)
            )
            """
        )
    monkeypatch.setattr(cutover.time, "time", lambda: 1000.0)
    monkeypatch.setattr(
        cutover,
        "_legacy_process_checkpoint_receipt",
        lambda _plan: {"status": "verified"},
    )

    receipt = cutover._legacy_durable_activity_receipt(
        {"legacy_state_db": str(state_db)}
    )

    assert receipt["webui_active_run_leases"] == 0
    assert receipt["expired_webui_activity_rows"] == 0


def test_legacy_durable_activity_rejects_future_heartbeat(
    tmp_path, monkeypatch
):
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as connection:
        connection.execute(
            """
            CREATE TABLE session_activity (
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                PRIMARY KEY (session_id, run_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO session_activity VALUES (?, ?, ?, ?, ?, ?)",
            ("sid", "run-future", "webui", "running", 990.0, 2000.0),
        )
    monkeypatch.setattr(cutover.time, "time", lambda: 1000.0)

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="legacy WebUI still has durable active-run leases",
    ):
        cutover._legacy_durable_activity_receipt(
            {"legacy_state_db": str(state_db)}
        )


def test_frozen_writer_verification_rejects_recorded_non_descendant(monkeypatch):
    prepared = {
        "legacy": {"pid": 10, "pid_start_token": "root-start"},
    }
    frozen = {
        "writers": [
            {
                "role": "webui",
                "status": "frozen",
                "tree": [
                    {
                        "pid": 10,
                        "ppid": None,
                        "pid_start_token": "root-start",
                        "state": "T",
                    },
                    {
                        "pid": 11,
                        "ppid": 10,
                        "pid_start_token": "child-start",
                        "state": "T",
                    },
                    {
                        "pid": 99,
                        "ppid": 1,
                        "pid_start_token": "foreign-start",
                        "state": "T",
                    },
                ],
            }
        ]
    }
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: True)
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "T")
    monkeypatch.setattr(
        cutover,
        "_process_parent_table",
        lambda: {10: 1, 11: 10, 99: 1},
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="membership changed",
    ):
        cutover._verify_frozen_prepared_writers({}, prepared, frozen)


def test_cutover_controller_starts_adopts_and_exactly_stops_ingress_gate(tmp_path):
    gate_script = Path(cutover.__file__).resolve().parents[2] / "evidence" / "ingress_gate.py"
    assert gate_script.is_file()
    control = tmp_path / "gate-control"
    control.mkdir(mode=0o700)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    plan = {
        "base_url": f"http://127.0.0.1:{port}",
        "listener_port": port,
        "timeout_seconds": 5,
        "interval_seconds": 0.02,
        "managed_interpreter": sys.executable,
        "ingress_gate_script": str(gate_script),
        "ingress_gate_expected_sha256": _sha(gate_script),
        "ingress_gate_token_file": str(control / "controller.token"),
        "ingress_gate_ready_receipt": str(control / "ready.json"),
    }

    started = cutover._start_or_adopt_ingress_gate(plan)
    try:
        assert started["status"] == "started"
        adopted = cutover._start_or_adopt_ingress_gate(plan)
        assert adopted["status"] == "adopted"
        assert adopted["binding"]["pid"] == started["binding"]["pid"]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(
                b"POST /api/chat/start HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            chunks = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            response = b"".join(chunks)
        assert response.startswith(b"HTTP/1.1 503")
        assert b"ingress_fenced" in response
    finally:
        stopped = cutover._stop_ingress_gate(plan, started)
    assert stopped["status"] == "stopped"
    assert not Path(plan["ingress_gate_ready_receipt"]).exists()
    rebound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rebound.bind(("127.0.0.1", port))
    finally:
        rebound.close()


def test_cutover_controller_retries_transient_ingress_gate_address_hold(
    tmp_path,
    monkeypatch,
):
    gate_script = tmp_path / "ingress_gate.py"
    gate_script.write_text("# retry probe\n", encoding="utf-8")
    control = tmp_path / "gate-control"
    control.mkdir(mode=0o700)
    plan = {
        "base_url": "http://127.0.0.1:18787",
        "listener_port": 18787,
        "timeout_seconds": 1,
        "interval_seconds": 0.001,
        "managed_interpreter": sys.executable,
        "ingress_gate_script": str(gate_script),
        "ingress_gate_expected_sha256": _sha(gate_script),
        "ingress_gate_token_file": str(control / "controller.token"),
        "ingress_gate_ready_receipt": str(control / "ready.json"),
    }
    processes = [
        SimpleNamespace(
            pid=101,
            stderr=io.BytesIO(
                b"ingress gate startup failed: [Errno 48] Address already in use\n"
            ),
            poll=lambda: 1,
            wait=lambda timeout: 1,
        ),
        SimpleNamespace(
            pid=102,
            stderr=io.BytesIO(),
            poll=lambda: None,
            wait=lambda timeout: 0,
        ),
    ]
    spawned = []

    def popen(*_args, **kwargs):
        spawned.append(kwargs)
        return processes[len(spawned) - 1]

    attestations = 0

    def attest(_plan):
        nonlocal attestations
        attestations += 1
        if attestations == 1:
            raise cutover.ReleaseBuildError("no gate yet")
        return {
            "status": "verified",
            "pid": 102,
            "pid_start_token": "gate-start",
        }

    monkeypatch.setattr(cutover.subprocess, "Popen", popen)
    monkeypatch.setattr(cutover, "_attest_ingress_gate", attest)
    monkeypatch.setattr(
        cutover,
        "_ingress_gate_listener_pid_or_none",
        lambda _port: None,
    )

    receipt = cutover._start_or_adopt_ingress_gate(plan)

    assert receipt["status"] == "started"
    assert receipt["binding"]["pid"] == 102
    assert len(spawned) == 2
    assert all(row["stderr"] == subprocess.PIPE for row in spawned)


def test_ingress_gate_token_receipt_creates_private_control_directory(
    tmp_path,
):
    token_path = tmp_path / "missing-control" / "controller.token"

    receipt = cutover._ingress_gate_token_receipt(
        {"ingress_gate_token_file": str(token_path)}
    )

    assert receipt["path"] == str(token_path)
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert token_path.read_text(encoding="ascii").strip() == receipt["token"]


def test_ingress_gate_token_receipt_rejects_existing_public_directory(
    tmp_path,
):
    control = tmp_path / "public-control"
    control.mkdir(mode=0o755)

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="ingress gate control directory is not private",
    ):
        cutover._ingress_gate_token_receipt(
            {"ingress_gate_token_file": str(control / "controller.token")}
        )


def test_ingress_gate_token_receipt_rejects_parent_replacement_during_create(
    tmp_path,
    monkeypatch,
):
    control = tmp_path / "control"
    diverted = tmp_path / "diverted-control"
    token_path = control / "controller.token"
    real_open = cutover.os.open
    swapped = False

    def swapping_open(target, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if (
            flags & os.O_CREAT
            and not swapped
            and (
                Path(target) == token_path
                or str(target) == token_path.name
            )
        ):
            control.rename(diverted)
            control.mkdir(mode=0o700)
            swapped = True
        return real_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(cutover.os, "open", swapping_open)

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="ingress gate control directory changed during token receipt",
    ):
        cutover._ingress_gate_token_receipt(
            {"ingress_gate_token_file": str(token_path)}
        )

    assert swapped is True
    assert not token_path.exists()
    assert (diverted / token_path.name).is_file()


def test_ingress_gate_token_receipt_closes_created_descriptor_if_fdopen_fails(
    tmp_path,
    monkeypatch,
):
    token_path = tmp_path / "control" / "controller.token"
    created_descriptor = None

    def failing_fdopen(descriptor, *_args, **_kwargs):
        nonlocal created_descriptor
        created_descriptor = descriptor
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(cutover.os, "fdopen", failing_fdopen)

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="ingress gate controller token cannot be written",
    ):
        cutover._ingress_gate_token_receipt(
            {"ingress_gate_token_file": str(token_path)}
        )

    assert created_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(created_descriptor)


def test_bootstrap_abort_boundary_remains_recoverable_after_job_bootout():
    assert cutover._can_restore_legacy_before_snapshot_abort(
        {"legacy_jobs_booted_out": {"status": "verified-absent"}}
    )
    assert not cutover._can_restore_legacy_before_snapshot_abort(
        {
            "legacy_jobs_booted_out": {"status": "verified-absent"},
            "services_stopped": {"status": "stopped"},
        }
    )
    assert not cutover._can_restore_legacy_before_snapshot_abort(
        {
            "legacy_jobs_booted_out": {"status": "verified-absent"},
            "rollback_started": {"error_type": "ReleaseBuildError"},
        }
    )


def test_incomplete_managed_webui_start_reconstructs_exact_stop_authorization(
    monkeypatch,
):
    selection = {
        "version": 2,
        "generation": 61,
        "current": "candidate-r24",
        "candidate": "candidate-r24",
        "pending_transaction_id": "bootstrap-transaction-000001",
        "last_good": "last-good",
        "bootstrap_fallback": "last-good",
        "release_root": "/tmp/releases",
        "releases": {},
    }
    runtime = {
        "pid": 41,
        "pid_start_token": "candidate-start",
        "program_identity": {"sha256": "a" * 64},
    }
    plan = {
        "transaction_id": "bootstrap-transaction-000001",
        "expected_candidate_identity": {
            "build_id": "candidate-r24",
            "selector_generation": 61,
        },
        "last_good_identity": {"build_id": "last-good"},
        "selector_state": "/tmp/selector.json",
        "selector_lock": "/tmp/selector.lock",
        "installed_plist": "/tmp/managed.plist",
    }
    journal = {
        "phases": {
            "managed_pair_start_intent": {
                "build_id": "candidate-r24",
                "selection": selection,
                "webui_install": {"sha256": "b" * 64},
            }
        }
    }
    monkeypatch.setattr(
        cutover.release_selector,
        "read_selector_state",
        lambda *_args, **_kwargs: copy.deepcopy(selection),
    )
    monkeypatch.setattr(cutover, "sha256_file", lambda _path: "b" * 64)
    monkeypatch.setattr(
        cutover,
        "_probe_startup_fenced_webui_binding",
        lambda _plan, _identity: {"runtime": runtime},
    )

    assert (
        cutover._incomplete_managed_webui_stop_authorization(plan, journal)
        == runtime
    )


def test_incomplete_managed_webui_start_rejects_selector_drift(monkeypatch):
    selection = {
        "generation": 61,
        "current": "candidate-r24",
        "candidate": "candidate-r24",
        "pending_transaction_id": "bootstrap-transaction-000001",
        "last_good": "last-good",
    }
    plan = {
        "transaction_id": "bootstrap-transaction-000001",
        "expected_candidate_identity": {
            "build_id": "candidate-r24",
            "selector_generation": 61,
        },
        "last_good_identity": {"build_id": "last-good"},
        "selector_state": "/tmp/selector.json",
        "selector_lock": "/tmp/selector.lock",
        "installed_plist": "/tmp/managed.plist",
    }
    journal = {
        "phases": {
            "managed_pair_start_intent": {
                "build_id": "candidate-r24",
                "selection": selection,
                "webui_install": {"sha256": "b" * 64},
            }
        }
    }
    monkeypatch.setattr(
        cutover.release_selector,
        "read_selector_state",
        lambda *_args, **_kwargs: {**selection, "generation": 62},
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="selector state changed",
    ):
        cutover._incomplete_managed_webui_stop_authorization(plan, journal)


def test_probe_startup_fenced_webui_does_not_require_mutating_deep_checks(
    monkeypatch,
):
    identity = {
        "build_id": "candidate-r24",
        "selector_generation": 61,
    }
    plan = {
        "listener_port": 8787,
        "base_url": "http://127.0.0.1:8787",
        "signing_key_file": "/tmp/release-control.key",
        "transaction_id": "bootstrap-transaction-000001",
        "timeout_seconds": 1,
    }
    binding = {
        "status": "verified",
        "signed_identity": identity,
        "deep_health": {
            "status": "ok",
            "checks": {
                "sessions": {"status": "deferred"},
                "projects": {"status": "deferred"},
                "state_db": {"status": "deferred"},
            },
        },
    }
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_job_pid", lambda _plan, *, gateway: 41)
    monkeypatch.setattr(cutover, "_read_release_control_key", lambda _path: b"key")
    monkeypatch.setattr(
        cutover,
        "_release_control_client",
        lambda *_args, **_kwargs: (
            lambda: {},
            lambda *_args, **_kwargs: {},
            plan["transaction_id"],
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_collect_process_binding",
        lambda _plan, *, inspect_control: binding,
    )

    def require_candidate(
        evidence,
        *,
        candidate_identity,
        expected_candidate_identity,
        admission_state,
        require_full_health,
    ):
        assert evidence is binding
        assert candidate_identity is identity
        assert expected_candidate_identity is identity
        assert admission_state == "startup-fenced"
        assert require_full_health is False
        return evidence

    monkeypatch.setattr(cutover, "_require_candidate_binding", require_candidate)

    assert cutover._probe_startup_fenced_webui_binding(plan, identity) is binding


def test_candidate_binding_accepts_mutation_free_deferred_startup_checks():
    expected = {
        "build_id": "candidate-r29",
        "manifest_sha256": "a" * 64,
        "agent_manifest_sha256": "b" * 64,
        "runtime_manifest_sha256": "c" * 64,
        "selector_generation": 73,
    }
    identity = {
        "pid": 41,
        "pid_start_token": "candidate-start",
    }
    evidence = {
        "status": "verified",
        "launchd_pid": 41,
        "listener_pid": 41,
        "signed_health_pid": 41,
        "pid_start_token": "candidate-start",
        "deep_health": {
            "status": "ok",
            "build": {
                "status": "managed",
                "valid": True,
                **expected,
            },
            "admission": {"state": "startup-fenced"},
            "checks": {
                "streams_lock": {"status": "ok"},
                "stream_runtime": {"status": "ok"},
                "startup_fence": {
                    "status": "fenced",
                    "mutation_free": True,
                },
                "sessions": {"status": "deferred"},
                "projects": {"status": "deferred"},
                "state_db": {"status": "deferred"},
            },
        },
    }

    assert cutover._require_candidate_binding(
        evidence,
        candidate_identity=identity,
        expected_candidate_identity=expected,
        admission_state="startup-fenced",
        require_full_health=True,
    ) is evidence


def test_candidate_binding_rejects_deferred_checks_after_admission_opens():
    expected = {
        "build_id": "candidate-r29",
        "manifest_sha256": "a" * 64,
        "agent_manifest_sha256": "b" * 64,
        "runtime_manifest_sha256": "c" * 64,
        "selector_generation": 73,
    }
    identity = {
        "pid": 41,
        "pid_start_token": "candidate-start",
    }
    evidence = {
        "status": "verified",
        "launchd_pid": 41,
        "listener_pid": 41,
        "signed_health_pid": 41,
        "pid_start_token": "candidate-start",
        "deep_health": {
            "status": "ok",
            "build": {
                "status": "managed",
                "valid": True,
                **expected,
            },
            "admission": {"state": "open"},
            "checks": {
                "streams_lock": {"status": "ok"},
                "stream_runtime": {"status": "ok"},
                "sessions": {"status": "deferred"},
                "projects": {"status": "deferred"},
                "state_db": {"status": "deferred"},
            },
        },
    }

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="deep health check failed: sessions",
    ):
        cutover._require_candidate_binding(
            evidence,
            candidate_identity=identity,
            expected_candidate_identity=expected,
            admission_state="open",
            require_full_health=True,
        )


def test_candidate_binding_accepts_full_health_behind_pair_gate_after_acceptance():
    expected = {
        "build_id": "candidate-r32",
        "manifest_sha256": "a" * 64,
        "agent_manifest_sha256": "b" * 64,
        "runtime_manifest_sha256": "c" * 64,
        "selector_generation": 82,
    }
    identity = {
        "pid": 41,
        "pid_start_token": "candidate-start",
    }
    evidence = {
        "status": "verified",
        "launchd_pid": 41,
        "listener_pid": 41,
        "signed_health_pid": 41,
        "pid_start_token": "candidate-start",
        "deep_health": {
            "status": "ok",
            "build": {
                "status": "managed",
                "valid": True,
                **expected,
            },
            "admission": {
                "state": "open",
                "effective_state": "pair-gated",
                "pair_gate": {
                    "status": "active",
                    "transaction_id": "pair-gate-transaction-000000000001",
                    "epoch": 82,
                    "owner_hash": "d" * 64,
                    "payload_sha256": "e" * 64,
                    "agent": {"build_id": "agent"},
                    "webui": {"build_id": expected["build_id"]},
                },
            },
            "checks": {
                "streams_lock": {"status": "ok"},
                "stream_runtime": {"status": "ok"},
                "sessions": {"status": "ok"},
                "projects": {"status": "ok"},
                "state_db": {"status": "missing"},
            },
        },
    }

    assert cutover._require_candidate_binding(
        evidence,
        candidate_identity=identity,
        expected_candidate_identity=expected,
        admission_state="open",
        require_full_health=True,
    ) is evidence


def test_bootstrap_journal_allows_exact_abort_after_job_bootout():
    phase_names = (
        "prepared",
        "pre_managed_controls_stage_intent",
        "pre_managed_controls_staged",
        "watchdog_cron_disabled",
        "writers_frozen",
        "cli_maintenance_gate_stage_intent",
        "cli_maintenance_gate_installed",
        "legacy_cron_tick_lock_normalize_intent",
        "legacy_cron_tick_lock_normalized",
        "legacy_cron_tick_lock_acquired",
        "legacy_gateway_drain_intent",
        "legacy_gateway_drain_acknowledged",
        "legacy_gateway_stop_intent",
        "legacy_gateway_gracefully_stopped",
        "synthetic_store_mode_normalize_intent",
        "synthetic_store_modes_normalized",
        "legacy_dispatcher_lock_acquired",
        "frozen_boundary_proved",
        "legacy_jobs_booted_out",
        "aborted_before_cutover",
    )
    transaction_id = "post-bootout-abort-transaction-000001"
    raw = {
        "version": 1,
        "transaction_id": transaction_id,
        "phases": {name: {} for name in phase_names},
    }

    validated = cutover._validated_bootstrap_journal(raw, transaction_id)

    assert "legacy_jobs_booted_out" in validated["phases"]
    assert "aborted_before_cutover" in validated["phases"]
    assert "rollback_started" not in validated["phases"]


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_drain_resets_on_activity_and_requires_final_identity():
    clock = _Clock()
    samples = iter(
        [
            {"active_runs": 0, "active_streams": 0, "pid": 10},  # t=0
            {"active_runs": 0, "active_streams": 0, "pid": 10},  # t=1
            {"active_runs": 1, "active_streams": 0, "pid": 10},  # t=2 reset
            {"active_runs": 0, "active_streams": 0, "pid": 10},  # t=3
            {"active_runs": 0, "active_streams": 0, "pid": 10},  # t=4
            {"active_runs": 0, "active_streams": 0, "pid": 10},  # t=5
            {"active_runs": 0, "active_streams": 0, "pid": 10},  # t=6
            {"active_runs": 0, "active_streams": 0, "pid": 10},  # final
        ]
    )

    final = cutover.wait_for_zero_activity(
        lambda: next(samples),
        identity_matches=lambda health: health["pid"] == 10,
        continuous_seconds=3,
        timeout_seconds=20,
        interval_seconds=1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert final["pid"] == 10
    assert clock.now == 6


def test_drain_final_identity_mismatch_fails_closed():
    clock = _Clock()

    with pytest.raises(cutover.DrainIdentityMismatch):
        cutover.wait_for_zero_activity(
            lambda: {"active_runs": 0, "active_streams": 0, "pid": 11},
            identity_matches=lambda health: health["pid"] == 10,
            continuous_seconds=2,
            timeout_seconds=10,
            interval_seconds=1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _release_repo(tmp_path: Path, name: str = "repo") -> tuple[Path, str]:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Hermes Release Test")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "remote", "add", "origin", "git@github.com:nesquena/hermes-webui.git")
    _write(repo / "bootstrap.py", "print('base')\n")
    _write(repo / "app.py", "VALUE = 1\n")
    _git(repo, "add", "bootstrap.py", "app.py")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo / "app.py", "VALUE = 2\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "candidate")
    return repo, base


def _agent_repo(tmp_path: Path, name: str = "agent-repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Hermes Agent Release Test")
    _git(repo, "config", "user.email", "agent-release-test@example.invalid")
    _git(repo, "remote", "add", "origin", "git@github.com:NousResearch/hermes-agent.git")
    _write(repo / "run_agent.py", "def main():\n    return 0\n")
    _write(repo / "agent" / "__init__.py", "# agent package\n")
    _write(repo / "hermes_cli" / "__init__.py", "# cli package\n")
    _write(repo / "tools" / "__init__.py", "# tools package\n")
    _write(repo / "tools" / "process_registry.py", "VERSION = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "agent base")
    _write(repo / "tools" / "process_registry.py", "VERSION = 2\n")
    _git(repo, "add", "tools/process_registry.py")
    _git(repo, "commit", "-q", "-m", "agent candidate")
    return repo


def test_sealed_runtime_builder_is_content_addressed_complete_and_read_only(
    monkeypatch, tmp_path, capsys
):
    python_home = tmp_path / "runtime-input" / "python-home"
    site_packages = tmp_path / "runtime-input" / "site-packages"
    _write(python_home / "bin" / "python3.11", "#!/bin/sh\nexit 0\n")
    _chmod(python_home / "bin" / "python3.11", 0o755)
    _write(python_home / "lib" / "python3.11" / "os.py", "# stdlib\n")
    _write(site_packages / "yaml" / "__init__.py", "VALUE = 1\n")
    _write(site_packages / "httpx" / "__init__.py", "VALUE = 1\n")
    _write(site_packages / "__pycache__" / "bad.pyc", "cache\n")
    _write(site_packages / "__editable___hermes_agent.pth", "/mutable/agent\n")
    _write(site_packages / "__editable___hermes_agent_finder.py", "MUTABLE = True\n")
    probes = []
    monkeypatch.setattr(
        cutover,
        "_probe_sealed_runtime",
        lambda identity, agent_identity: probes.append(
            (identity, agent_identity["manifest_sha256"])
        ),
    )
    agent_identity = _agent_source_snapshot(tmp_path)["identity"]

    identity = cutover.build_immutable_runtime(
        python_home,
        site_packages,
        release_root=tmp_path / "runtime-releases",
        interpreter_relative_path="bin/python3.11",
        agent_source_identity=agent_identity,
    )

    runtime_path = Path(identity["path"])
    assert runtime_path.name == identity["manifest_sha256"]
    assert Path(identity["interpreter_path"]) == (
        runtime_path / "python-home" / "bin" / "python3.11"
    )
    assert Path(identity["site_packages_path"]) == runtime_path / "site-packages"
    assert not (runtime_path / "site-packages" / "__pycache__").exists()
    assert not (runtime_path / "site-packages" / "__editable___hermes_agent.pth").exists()
    assert not (
        runtime_path / "site-packages" / "__editable___hermes_agent_finder.py"
    ).exists()
    assert not stat.S_IMODE(runtime_path.stat().st_mode) & 0o222
    assert probes and probes[0][1] == agent_identity["manifest_sha256"]
    assert selector.verify_runtime(identity)["manifest_sha256"] == identity[
        "manifest_sha256"
    ]

    stdlib = runtime_path / "python-home" / "lib" / "python3.11" / "os.py"
    _chmod(stdlib, 0o644)
    _write(stdlib, "TAMPERED = True\n")
    _chmod(stdlib, 0o444)
    with pytest.raises(selector.SelectorError, match="runtime.*hash"):
        selector.verify_runtime(identity)

    agent_json = tmp_path / "agent-identity.json"
    agent_json.write_text(json.dumps(agent_identity))
    assert cutover.main(
        [
            "build-runtime",
            "--python-home",
            str(python_home),
            "--site-packages",
            str(site_packages),
            "--release-root",
            str(tmp_path / "cli-runtime-releases"),
            "--interpreter-relative-path",
            "bin/python3.11",
            "--agent-source-identity-json",
            str(agent_json),
        ]
    ) == 0
    cli_identity = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert selector.verify_runtime(cli_identity)["manifest_sha256"] == cli_identity[
        "manifest_sha256"
    ]


def test_runtime_builder_rejects_external_symlinks_and_fsyncs_every_file(
    monkeypatch, tmp_path
):
    python_home = tmp_path / "input" / "python-home"
    site_packages = tmp_path / "input" / "site-packages"
    _write(python_home / "bin" / "python3.11", "#!/bin/sh\nexit 0\n")
    _chmod(python_home / "bin" / "python3.11", 0o755)
    _write(site_packages / "safe.py", "SAFE = True\n")
    external = tmp_path / "external.py"
    _write(external, "EXTERNAL = True\n")
    (site_packages / "escape.py").symlink_to(external)
    monkeypatch.setattr(cutover, "_probe_sealed_runtime", lambda *_args: None)
    agent_identity = _agent_source_snapshot(tmp_path)["identity"]

    with pytest.raises(cutover.ReleaseBuildError, match="symlink.*escapes"):
        cutover.build_immutable_runtime(
            python_home,
            site_packages,
            release_root=tmp_path / "rejected",
            interpreter_relative_path="bin/python3.11",
            agent_source_identity=agent_identity,
        )

    (site_packages / "escape.py").unlink()
    fsynced = []
    real_fsync_file = cutover._fsync_runtime_file
    monkeypatch.setattr(
        cutover,
        "_fsync_runtime_file",
        lambda path: fsynced.append(Path(path).name) or real_fsync_file(path),
    )
    identity = cutover.build_immutable_runtime(
        python_home,
        site_packages,
        release_root=tmp_path / "accepted",
        interpreter_relative_path="bin/python3.11",
        agent_source_identity=agent_identity,
    )
    assert sorted(fsynced) == ["python3.11", "safe.py"]
    assert selector.verify_runtime(identity)["manifest_sha256"] == identity[
        "manifest_sha256"
    ]


def test_sealed_runtime_probe_asserts_all_critical_origins(monkeypatch, tmp_path):
    runtime = _runtime_snapshot(tmp_path)["identity"]
    agent = _agent_source_snapshot(tmp_path)["identity"]
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cutover.subprocess, "run", fake_run)
    cutover._probe_sealed_runtime(runtime, agent)

    probe = captured["argv"][-1]
    assert captured["argv"][:2] == [runtime["interpreter_path"], "-S"]
    assert captured["env"]["PYTHONHOME"] == runtime["python_home_path"]
    assert captured["env"]["PYTHONPATH"] == os.pathsep.join(
        [agent["path"], runtime["site_packages_path"]]
    )
    for evidence in (
        "sys.executable",
        "sys.prefix",
        "sys.base_prefix",
        "os.__file__",
        "encodings.__file__",
        "run_agent.__file__",
        "tools.process_registry.__file__",
    ):
        assert evidence in probe


def test_release_and_launch_paths_bind_only_the_sealed_runtime(tmp_path):
    release = _managed_release(tmp_path)
    old_interpreter = tmp_path / "legacy-venv" / "bin" / "python"
    old_target = tmp_path / "legacy" / "bootstrap.py"
    _write(old_interpreter, "legacy mutable python\n")
    _write(old_target, "print('legacy')\n")
    _chmod(old_interpreter, 0o755)
    state_path = tmp_path / "control" / "selector.json"
    lock_path = tmp_path / "control" / "selector.lock"
    _write(state_path, "{}\n")
    _write(lock_path, "")
    original = {
        "Label": "com.example.webui",
        "ProgramArguments": [
            str(old_interpreter),
            str(old_target),
            "--foreground",
        ],
    }
    identity = selector.verify_release(
        release["release_path"],
        release_root=release["release_root"],
        expected_manifest_sha256=release["manifest_sha256"],
        selector_path=release["selector_path"],
    )
    assert identity["runtime_manifest_sha256"] == release["runtime"]["identity"][
        "manifest_sha256"
    ]

    selector_plist = cutover.transform_launchd_target(
        original,
        str(release["selector_path"]),
        expected_label="com.example.webui",
        expected_old_interpreter=str(old_interpreter),
        managed_interpreter=str(release["interpreter_path"]),
        expected_old_target=str(old_target),
        selector_state_path=str(state_path),
        selector_lock_path=str(lock_path),
    )
    assert selector_plist["ProgramArguments"][:3] == [
        str(release["interpreter_path"]),
        "-S",
        str(release["selector_path"]),
    ]
    assert selector_plist["ProgramArguments"][7:9] == [
        "--launchd-label",
        "com.example.webui",
    ]

    transaction_id = "direct-fallback-transaction-000001"
    fallback = cutover.build_direct_fallback_plist(
        original,
        expected_label="com.example.webui",
        expected_old_interpreter=str(old_interpreter),
        expected_old_target=str(old_target),
        release_identity=identity,
        selector_generation=9,
        selector_state_path=str(state_path),
        selector_lock_path=str(lock_path),
        startup_transaction_id=transaction_id,
    )
    assert fallback["ProgramArguments"][:3] == [
        str(release["interpreter_path"]),
        "-S",
        str(release["release_path"] / "bootstrap.py"),
    ]
    environment = fallback["EnvironmentVariables"]
    assert environment["HERMES_WEBUI_STARTUP_FENCED"] == "1"
    assert environment["HERMES_WEBUI_STARTUP_TRANSACTION_ID"] == transaction_id
    assert environment["PYTHONHOME"] == release["runtime"]["identity"][
        "python_home_path"
    ]
    assert environment["PYTHONPATH"] == os.pathsep.join(
        [
            release["agent_source"]["identity"]["path"],
            release["runtime"]["identity"]["site_packages_path"],
        ]
    )
    _write(old_interpreter, "MUTATED LEGACY VENV\n")
    assert fallback["ProgramArguments"][0] != str(old_interpreter)


def test_release_builder_embeds_and_requires_the_sealed_runtime(tmp_path):
    repo, base = _release_repo(tmp_path)
    runtime = _runtime_snapshot(tmp_path)["identity"]
    external_selector = tmp_path / "control" / "selector.py"
    _write(external_selector, "# selector\n")
    _chmod(external_selector, 0o755)
    interpreter = Path(runtime["interpreter_path"])
    built = cutover.build_immutable_release(
        repo,
        "HEAD",
        release_root=tmp_path / "releases",
        build_id="candidate",
        base_ref=base,
        expected_base_commit=base,
        expected_origin_url="git@github.com:nesquena/hermes-webui.git",
        allowed_changed_paths={"app.py"},
        selector_path=external_selector,
        interpreter_path=interpreter,
        expected_selector_identity=_identity_receipt(external_selector),
        expected_interpreter_identity=_identity_receipt(interpreter),
        runtime_identity=runtime,
        agent_source_identity=_agent_source_snapshot(tmp_path)["identity"],
        metadata=_release_metadata({"app.py"}),
    )
    manifest = json.loads(
        (Path(built["release_path"]) / selector.MANIFEST_NAME).read_text()
    )
    assert manifest["runtime"] == runtime

    mutable_interpreter = tmp_path / "mutable-venv" / "python"
    _write(mutable_interpreter, "mutable\n")
    _chmod(mutable_interpreter, 0o755)
    with pytest.raises(cutover.ReleaseBuildError, match="sealed runtime"):
        cutover.build_immutable_release(
            repo,
            "HEAD",
            release_root=tmp_path / "rejected-releases",
            build_id="rejected",
            base_ref=base,
            expected_base_commit=base,
            expected_origin_url="git@github.com:nesquena/hermes-webui.git",
            allowed_changed_paths={"app.py"},
            selector_path=external_selector,
            interpreter_path=mutable_interpreter,
            expected_selector_identity=_identity_receipt(external_selector),
            expected_interpreter_identity=_identity_receipt(mutable_interpreter),
            runtime_identity=runtime,
            agent_source_identity=_agent_source_snapshot(tmp_path)["identity"],
            metadata=_release_metadata({"app.py"}),
        )


def test_agent_builder_requires_exact_product_ancestry_diff_and_layout(tmp_path):
    repo = _agent_repo(tmp_path)
    common = {
        "release_root": tmp_path / "installed-agent-releases",
        "expected_origin_url": "git@github.com:NousResearch/hermes-agent.git",
        "base_ref": "HEAD^",
        "expected_base_commit": _git(repo, "rev-parse", "HEAD^"),
        "allowed_changed_paths": {"tools/process_registry.py"},
    }

    identity = cutover.build_immutable_agent_source(repo, "HEAD", **common)
    manifest = json.loads(Path(identity["manifest_path"]).read_text())
    assert manifest["origin_url"] == common["expected_origin_url"]
    assert manifest["base_commit"] == _git(repo, "rev-parse", "HEAD^")
    assert manifest["changed_files"] == ["tools/process_registry.py"]

    wrong_origin = {**common, "release_root": tmp_path / "wrong-origin"}
    wrong_origin["expected_origin_url"] = "git@example.invalid:wrong/agent.git"
    with pytest.raises(cutover.ReleaseBuildError, match="origin"):
        cutover.build_immutable_agent_source(repo, "HEAD", **wrong_origin)

    wrong_ancestry = {
        **common,
        "release_root": tmp_path / "wrong-ancestry",
        "base_ref": "HEAD",
        "expected_base_commit": _git(repo, "rev-parse", "HEAD"),
    }
    with pytest.raises(cutover.ReleaseBuildError, match="ancestor"):
        cutover.build_immutable_agent_source(repo, "HEAD^", **wrong_ancestry)

    wrong_diff = {
        **common,
        "release_root": tmp_path / "wrong-diff",
        "allowed_changed_paths": {"run_agent.py"},
    }
    with pytest.raises(cutover.ReleaseBuildError, match="changed paths"):
        cutover.build_immutable_agent_source(repo, "HEAD", **wrong_diff)

    _git(repo, "rm", "hermes_cli/__init__.py")
    _git(repo, "commit", "-q", "-m", "break required topology")
    incomplete = {
        **common,
        "release_root": tmp_path / "incomplete",
        "base_ref": "HEAD^",
        "expected_base_commit": _git(repo, "rev-parse", "HEAD^"),
        "allowed_changed_paths": {"hermes_cli/__init__.py"},
    }
    with pytest.raises(cutover.ReleaseBuildError, match="layout"):
        cutover.build_immutable_agent_source(repo, "HEAD", **incomplete)


def test_agent_source_builder_freezes_content_addressed_readonly_snapshot(tmp_path):
    repo = _agent_repo(tmp_path)
    release_root = tmp_path / "installed-agent-releases"
    base = _git(repo, "rev-parse", "HEAD^")

    identity = cutover.build_immutable_agent_source(
        repo,
        "HEAD",
        release_root=release_root,
        expected_origin_url="git@github.com:NousResearch/hermes-agent.git",
        base_ref="HEAD^",
        expected_base_commit=base,
        allowed_changed_paths={"tools/process_registry.py"},
    )

    source_path = Path(identity["path"])
    manifest_path = Path(identity["manifest_path"])
    assert source_path.name == identity["manifest_sha256"]
    assert manifest_path.name == f"{identity['manifest_sha256']}.json"
    assert source_path.parent == release_root / "snapshots"
    assert manifest_path.parent == release_root / "manifests"
    assert not stat.S_IMODE(source_path.stat().st_mode) & 0o222
    assert not stat.S_IMODE((source_path / "run_agent.py").stat().st_mode) & 0o222
    assert not stat.S_IMODE(manifest_path.stat().st_mode) & 0o222
    assert selector.verify_agent_source(identity)["manifest_sha256"] == identity[
        "manifest_sha256"
    ]


@pytest.mark.parametrize("mutation", ["dirty", "symlink"])
def test_agent_source_builder_rejects_uncommitted_or_symlinked_source(
    tmp_path, mutation
):
    repo = _agent_repo(tmp_path)
    if mutation == "dirty":
        _write(repo / "untracked.py", "DIRTY = True\n")
    else:
        (repo / "linked.py").symlink_to("run_agent.py")
        _git(repo, "add", "linked.py")
        _git(repo, "commit", "-q", "-m", "tracked symlink")
    base = _git(repo, "rev-parse", "HEAD^")
    allowed = {"linked.py"} if mutation == "symlink" else {"tools/process_registry.py"}

    with pytest.raises(cutover.ReleaseBuildError, match=mutation):
        cutover.build_immutable_agent_source(
            repo,
            "HEAD",
            release_root=tmp_path / "installed-agent-releases",
            expected_origin_url="git@github.com:NousResearch/hermes-agent.git",
            base_ref="HEAD^",
            expected_base_commit=base,
            allowed_changed_paths=allowed,
        )


def test_cutover_cli_builds_agent_source_identity(tmp_path, capsys):
    repo = _agent_repo(tmp_path)

    assert (
        cutover.main(
            [
                "build-agent-source",
                "--repo",
                str(repo),
                "--ref",
                "HEAD",
                "--release-root",
                str(tmp_path / "installed-agent-releases"),
                "--expected-origin-url",
                "git@github.com:NousResearch/hermes-agent.git",
                "--base-ref",
                "HEAD^",
                "--expected-base-commit",
                _git(repo, "rev-parse", "HEAD^"),
                "--allowed-changed-path",
                "tools/process_registry.py",
            ]
        )
        == 0
    )

    identity = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert selector.verify_agent_source(identity)["commit"] == _git(
        repo, "rev-parse", "HEAD"
    )


def test_release_builder_enforces_admission_and_builds_readonly(tmp_path):
    repo, base = _release_repo(tmp_path)
    external_selector = tmp_path / "control" / "selector.py"
    runtime, interpreter = _sealed_runtime_for_build(tmp_path)
    _write(external_selector, "# external selector\n")
    _chmod(external_selector, 0o755)
    selector_receipt = _identity_receipt(external_selector)
    interpreter_receipt = _identity_receipt(interpreter)
    release_root = tmp_path / "installed-releases"

    with pytest.raises(cutover.ReleaseBuildError, match="admission"):
        cutover.build_immutable_release(
            repo,
            "HEAD",
            release_root=release_root,
            build_id="rejected",
            base_ref=base,
            expected_base_commit=base,
            expected_origin_url="git@github.com:nesquena/hermes-webui.git",
            allowed_changed_paths=set(),
            selector_path=external_selector,
            interpreter_path=interpreter,
            expected_selector_identity=selector_receipt,
            expected_interpreter_identity=interpreter_receipt,
            runtime_identity=runtime,
            agent_source_identity=_agent_source_snapshot(tmp_path)["identity"],
            metadata=_release_metadata({"app.py"}),
        )

    built = cutover.build_immutable_release(
        repo,
        "HEAD",
        release_root=release_root,
        build_id="candidate",
        base_ref=base,
        expected_base_commit=base,
        expected_origin_url="git@github.com:nesquena/hermes-webui.git",
        allowed_changed_paths={"app.py"},
        selector_path=external_selector,
        interpreter_path=interpreter,
        expected_selector_identity=selector_receipt,
        expected_interpreter_identity=interpreter_receipt,
        runtime_identity=runtime,
        agent_source_identity=_agent_source_snapshot(tmp_path)["identity"],
        metadata=_release_metadata({"app.py"}),
    )

    release_path = release_root / "candidate"
    manifest = json.loads((release_path / selector.MANIFEST_NAME).read_text())
    assert built["release_path"] == str(release_path.resolve())
    assert manifest["base_commit"] == base
    assert manifest["changed_files"] == ["app.py"]
    assert manifest["agent_source"] == _agent_source_snapshot(tmp_path)["identity"]
    assert manifest["test_receipts"][0]["status"] == "passed"
    assert not stat.S_IMODE((release_path / "app.py").stat().st_mode) & 0o222
    assert not stat.S_IMODE(release_path.stat().st_mode) & 0o222
    selector.verify_release(
        release_path,
        release_root=release_root,
        expected_manifest_sha256=built["manifest_sha256"],
        selector_path=external_selector,
    )


def test_webui_builder_requires_exact_product_origin_ancestry_and_diff(tmp_path):
    repo, base = _release_repo(tmp_path)
    external_selector = tmp_path / "control" / "selector.py"
    runtime, interpreter = _sealed_runtime_for_build(tmp_path)
    _write(external_selector, "# selector\n")
    _chmod(external_selector, 0o755)
    common = {
        "repo": repo,
        "ref": "HEAD",
        "build_id": "candidate",
        "base_ref": base,
        "allowed_changed_paths": {"app.py"},
        "selector_path": external_selector,
        "interpreter_path": interpreter,
        "expected_selector_identity": _identity_receipt(external_selector),
        "expected_interpreter_identity": _identity_receipt(interpreter),
        "runtime_identity": runtime,
        "agent_source_identity": _agent_source_snapshot(tmp_path)["identity"],
        "metadata": _release_metadata({"app.py"}),
        "expected_origin_url": "git@github.com:nesquena/hermes-webui.git",
        "expected_base_commit": base,
    }

    built = cutover.build_immutable_release(
        release_root=tmp_path / "valid-release",
        **common,
    )
    manifest = json.loads(
        (Path(built["release_path"]) / selector.MANIFEST_NAME).read_text()
    )
    assert manifest["origin_url"] == common["expected_origin_url"]

    wrong_origin = {**common, "build_id": "wrong-origin"}
    wrong_origin["expected_origin_url"] = "git@example.invalid:wrong/webui.git"
    with pytest.raises(cutover.ReleaseBuildError, match="origin"):
        cutover.build_immutable_release(
            release_root=tmp_path / "wrong-origin-release",
            **wrong_origin,
        )

    wrong_ancestry = {
        **common,
        "ref": base,
        "base_ref": "HEAD",
        "expected_base_commit": _git(repo, "rev-parse", "HEAD"),
        "build_id": "wrong-ancestry",
        "allowed_changed_paths": {"app.py"},
    }
    with pytest.raises(cutover.ReleaseBuildError, match="ancestor"):
        cutover.build_immutable_release(
            release_root=tmp_path / "wrong-ancestry-release",
            **wrong_ancestry,
        )

    wrong_diff = {
        **common,
        "build_id": "wrong-diff",
        "allowed_changed_paths": {"app.py", "not-changed.py"},
    }
    with pytest.raises(cutover.ReleaseBuildError, match="changed paths"):
        cutover.build_immutable_release(
            release_root=tmp_path / "wrong-diff-release",
            **wrong_diff,
        )


def test_product_builders_ignore_git_replacement_object_attacks(tmp_path):
    agent_repo = _agent_repo(tmp_path, "agent")
    agent_target = _git(agent_repo, "rev-parse", "HEAD")
    agent_base = _git(agent_repo, "rev-parse", "HEAD^")
    _write(agent_repo / "tools" / "process_registry.py", "MALICIOUS = True\n")
    _git(agent_repo, "add", "tools/process_registry.py")
    _git(agent_repo, "commit", "-q", "-m", "replacement payload")
    _git(agent_repo, "replace", agent_target, "HEAD")

    agent_identity = cutover.build_immutable_agent_source(
        agent_repo,
        agent_target,
        release_root=tmp_path / "agent-releases",
        expected_origin_url="git@github.com:NousResearch/hermes-agent.git",
        base_ref=agent_base,
        expected_base_commit=agent_base,
        allowed_changed_paths={"tools/process_registry.py"},
    )
    assert (
        Path(agent_identity["path"]) / "tools" / "process_registry.py"
    ).read_text() == "VERSION = 2\n"

    webui_repo, webui_base = _release_repo(tmp_path, "webui")
    webui_target = _git(webui_repo, "rev-parse", "HEAD")
    _write(webui_repo / "app.py", "MALICIOUS = True\n")
    _git(webui_repo, "add", "app.py")
    _git(webui_repo, "commit", "-q", "-m", "replacement payload")
    _git(webui_repo, "replace", webui_target, "HEAD")
    external_selector = tmp_path / "control" / "selector.py"
    runtime, interpreter = _sealed_runtime_for_build(tmp_path)
    _write(external_selector, "# selector\n")
    _chmod(external_selector, 0o755)

    built = cutover.build_immutable_release(
        webui_repo,
        webui_target,
        release_root=tmp_path / "webui-releases",
        build_id="candidate",
        base_ref=webui_base,
        expected_base_commit=webui_base,
        expected_origin_url="git@github.com:nesquena/hermes-webui.git",
        allowed_changed_paths={"app.py"},
        selector_path=external_selector,
        interpreter_path=interpreter,
        expected_selector_identity=_identity_receipt(external_selector),
        expected_interpreter_identity=_identity_receipt(interpreter),
        runtime_identity=runtime,
        agent_source_identity=_agent_source_snapshot(tmp_path)["identity"],
        metadata=_release_metadata({"app.py"}),
    )
    assert (Path(built["release_path"]) / "app.py").read_text() == "VALUE = 2\n"


def test_release_builder_reverifies_agent_source_before_embedding(tmp_path):
    repo, base = _release_repo(tmp_path)
    external_selector = tmp_path / "control" / "selector.py"
    runtime, interpreter = _sealed_runtime_for_build(tmp_path)
    _write(external_selector, "# external selector\n")
    _chmod(external_selector, 0o755)
    agent_identity = _agent_source_snapshot(tmp_path)["identity"]
    run_agent = Path(agent_identity["path"]) / "run_agent.py"
    _chmod(run_agent, 0o644)
    _write(run_agent, "DRIFTED = True\n")
    _chmod(run_agent, 0o444)

    with pytest.raises(cutover.ReleaseBuildError, match="agent source"):
        cutover.build_immutable_release(
            repo,
            "HEAD",
            release_root=tmp_path / "installed-releases",
            build_id="candidate",
            base_ref=base,
            expected_base_commit=base,
            expected_origin_url="git@github.com:nesquena/hermes-webui.git",
            allowed_changed_paths={"app.py"},
            selector_path=external_selector,
            interpreter_path=interpreter,
            expected_selector_identity=_identity_receipt(external_selector),
            expected_interpreter_identity=_identity_receipt(interpreter),
            runtime_identity=runtime,
            agent_source_identity=agent_identity,
            metadata=_release_metadata({"app.py"}),
        )


def test_release_builder_fsyncs_read_only_modes_before_publish(monkeypatch, tmp_path):
    repo, base = _release_repo(tmp_path)
    agent_source_identity = _agent_source_snapshot(tmp_path)["identity"]
    runtime = _runtime_snapshot(tmp_path)["identity"]
    external_selector = tmp_path / "control" / "selector.py"
    interpreter = Path(runtime["interpreter_path"])
    _write(external_selector, "# selector\n")
    _chmod(external_selector, 0o755)
    events = []
    real_fchmod = cutover.os.fchmod
    real_fsync = cutover.os.fsync
    real_chmod = cutover.os.chmod
    real_directory_fsync = cutover._fsync_directory

    def recording_fchmod(descriptor, mode):
        events.append(("fchmod", descriptor, mode))
        return real_fchmod(descriptor, mode)

    def recording_fsync(descriptor):
        events.append(("fsync", descriptor))
        return real_fsync(descriptor)

    def recording_chmod(path, mode):
        events.append(("chmod", str(path), mode))
        return real_chmod(path, mode)

    def recording_directory_fsync(path):
        events.append(("directory_fsync", str(path)))
        return real_directory_fsync(path)

    monkeypatch.setattr(cutover.os, "fchmod", recording_fchmod)
    monkeypatch.setattr(cutover.os, "fsync", recording_fsync)
    monkeypatch.setattr(cutover.os, "chmod", recording_chmod)
    monkeypatch.setattr(cutover, "_fsync_directory", recording_directory_fsync)

    cutover.build_immutable_release(
        repo,
        "HEAD",
        release_root=tmp_path / "installed-releases",
        build_id="candidate",
        base_ref=base,
        expected_base_commit=base,
        expected_origin_url="git@github.com:nesquena/hermes-webui.git",
        allowed_changed_paths={"app.py"},
        selector_path=external_selector,
        interpreter_path=interpreter,
        expected_selector_identity=_identity_receipt(external_selector),
        expected_interpreter_identity=_identity_receipt(interpreter),
        runtime_identity=runtime,
        agent_source_identity=agent_source_identity,
        metadata=_release_metadata({"app.py"}),
    )

    for index, event in enumerate(events):
        if event[0] == "fchmod" and event[2] in {0o444, 0o555}:
            assert ("fsync", event[1]) in events[index + 1 :]
        if event[0] == "chmod" and event[2] == 0o555 and ".candidate." in event[1]:
            assert ("directory_fsync", event[1]) in events[index + 1 :]


def test_release_builder_rejects_tracked_symlink(tmp_path):
    repo, base = _release_repo(tmp_path)
    (repo / "linked.py").symlink_to("app.py")
    _git(repo, "add", "linked.py")
    _git(repo, "commit", "-q", "-m", "symlink")
    runtime = _runtime_snapshot(tmp_path)["identity"]
    external_selector = tmp_path / "control" / "selector.py"
    interpreter = Path(runtime["interpreter_path"])
    _write(external_selector, "# external selector\n")
    _chmod(external_selector, 0o755)

    with pytest.raises(cutover.ReleaseBuildError, match="symlink"):
        cutover.build_immutable_release(
            repo,
            "HEAD",
            release_root=tmp_path / "installed-releases",
            build_id="candidate",
            base_ref=base,
            expected_base_commit=base,
            expected_origin_url="git@github.com:nesquena/hermes-webui.git",
            allowed_changed_paths={"app.py", "linked.py"},
            selector_path=external_selector,
            interpreter_path=interpreter,
            expected_selector_identity=_identity_receipt(external_selector),
            expected_interpreter_identity=_identity_receipt(interpreter),
            runtime_identity=runtime,
            agent_source_identity=_agent_source_snapshot(tmp_path)["identity"],
            metadata=_release_metadata({"app.py", "linked.py"}),
        )


def test_release_builder_rejects_relative_root_and_nonexecutable_runtime(
    monkeypatch, tmp_path
):
    repo, base = _release_repo(tmp_path)
    runtime = _runtime_snapshot(tmp_path)["identity"]
    external_selector = tmp_path / "control" / "selector.py"
    interpreter = Path(runtime["interpreter_path"])
    _write(external_selector, "# external selector\n")
    _chmod(external_selector, 0o755)
    _chmod(interpreter, 0o444)
    selector_receipt = _identity_receipt(external_selector)
    interpreter_receipt = _identity_receipt(interpreter)

    with pytest.raises(cutover.ReleaseBuildError, match="runtime interpreter"):
        cutover.build_immutable_release(
            repo,
            "HEAD",
            release_root=tmp_path / "installed-releases",
            build_id="candidate",
            base_ref=base,
            expected_base_commit=base,
            expected_origin_url="git@github.com:nesquena/hermes-webui.git",
            allowed_changed_paths={"app.py"},
            selector_path=external_selector,
            interpreter_path=interpreter,
            expected_selector_identity=selector_receipt,
            expected_interpreter_identity=interpreter_receipt,
            runtime_identity=runtime,
            agent_source_identity=_agent_source_snapshot(tmp_path)["identity"],
            metadata=_release_metadata({"app.py"}),
        )

    _chmod(interpreter, 0o755)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(cutover.ReleaseBuildError, match="absolute"):
        cutover.build_immutable_release(
            repo,
            "HEAD",
            release_root=Path("installed-releases"),
            build_id="candidate",
            base_ref=base,
            expected_base_commit=base,
            expected_origin_url="git@github.com:nesquena/hermes-webui.git",
            allowed_changed_paths={"app.py"},
            selector_path=external_selector,
            interpreter_path=interpreter,
            expected_selector_identity=selector_receipt,
            expected_interpreter_identity=_identity_receipt(interpreter),
            runtime_identity=runtime,
            agent_source_identity=_agent_source_snapshot(tmp_path)["identity"],
            metadata=_release_metadata({"app.py"}),
        )


def test_release_builder_rejects_external_identity_not_matching_frozen_receipt(
    tmp_path,
):
    repo, base = _release_repo(tmp_path)
    runtime = _runtime_snapshot(tmp_path)["identity"]
    external_selector = tmp_path / "control" / "selector.py"
    interpreter = Path(runtime["interpreter_path"])
    _write(external_selector, "# expected selector\n")
    _chmod(external_selector, 0o755)
    frozen_selector = _identity_receipt(external_selector)
    _write(external_selector, "# tampered before build\n")
    _chmod(external_selector, 0o755)

    with pytest.raises(cutover.ReleaseBuildError, match="frozen identity"):
        cutover.build_immutable_release(
            repo,
            "HEAD",
            release_root=tmp_path / "installed-releases",
            build_id="candidate",
            base_ref=base,
            expected_base_commit=base,
            expected_origin_url="git@github.com:nesquena/hermes-webui.git",
            allowed_changed_paths={"app.py"},
            selector_path=external_selector,
            interpreter_path=interpreter,
            expected_selector_identity=frozen_selector,
            expected_interpreter_identity=_identity_receipt(interpreter),
            runtime_identity=runtime,
            agent_source_identity=_agent_source_snapshot(tmp_path)["identity"],
            metadata=_release_metadata({"app.py"}),
        )


def test_release_builder_ignores_repository_shaping_git_environment(
    monkeypatch, tmp_path
):
    repo_a, base_a = _release_repo(tmp_path, "repo-a")
    repo_b, _base_b = _release_repo(tmp_path, "repo-b")
    _write(repo_b / "app.py", "VALUE = 999\n")
    _git(repo_b, "add", "app.py")
    _git(repo_b, "commit", "-q", "-m", "poison")
    runtime = _runtime_snapshot(tmp_path)["identity"]
    external_selector = tmp_path / "control" / "selector.py"
    interpreter = Path(runtime["interpreter_path"])
    _write(external_selector, "# selector\n")
    _chmod(external_selector, 0o755)
    expected_commit = _git(repo_a, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(repo_b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo_b))

    built = cutover.build_immutable_release(
        repo_a,
        "HEAD",
        release_root=tmp_path / "installed-releases",
        build_id="candidate",
        base_ref=base_a,
        expected_base_commit=base_a,
        expected_origin_url="git@github.com:nesquena/hermes-webui.git",
        allowed_changed_paths={"app.py"},
        selector_path=external_selector,
        interpreter_path=interpreter,
        expected_selector_identity=_identity_receipt(external_selector),
        expected_interpreter_identity=_identity_receipt(interpreter),
        runtime_identity=runtime,
        agent_source_identity=_agent_source_snapshot(tmp_path)["identity"],
        metadata=_release_metadata({"app.py"}),
    )

    assert built["commit"] == expected_commit
    assert (Path(built["release_path"]) / "app.py").read_text() == "VALUE = 2\n"


def _pre_managed_control_plan(tmp_path: Path) -> dict:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    return {
        "transaction_id": "pre-managed-rollback-transaction-0001",
        "transaction_journal": str(control / "transaction.json"),
        "selector_state": str(control / "selector-state.json"),
        "selector_lock": str(control / "selector-state.lock"),
        "managed_plist": str(control / "managed.plist"),
    }


@pytest.mark.parametrize("preexisting", [False, True])
def test_pre_managed_control_restore_is_exact_for_present_and_absent_state(
    tmp_path,
    preexisting,
):
    plan = _pre_managed_control_plan(tmp_path)
    original = {
        "selector_state": b'{"before":"selector"}\n',
        "selector_lock": b"before-lock\n",
        "managed_plist": b"before-plist\n",
    }
    if preexisting:
        for key, payload in original.items():
            path = Path(plan[key])
            path.write_bytes(payload)
            path.chmod(0o600)

    captured = cutover._capture_pre_managed_control_state(plan)
    for key in original:
        path = Path(plan[key])
        path.write_bytes(f"owned-{key}\n".encode())
        path.chmod(0o600)
    staged = cutover._pre_managed_control_stage_receipt(plan)

    restored = cutover._restore_pre_managed_control_state(
        plan,
        captured,
        staged,
    )

    assert restored["status"] == "restored"
    for key, payload in original.items():
        path = Path(plan[key])
        if preexisting:
            assert path.read_bytes() == payload
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        else:
            assert not path.exists()


def test_pre_managed_control_restore_refuses_non_owned_mutation(tmp_path):
    plan = _pre_managed_control_plan(tmp_path)
    captured = cutover._capture_pre_managed_control_state(plan)
    for key in ("selector_state", "selector_lock", "managed_plist"):
        path = Path(plan[key])
        path.write_bytes(f"owned-{key}\n".encode())
        path.chmod(0o600)
    staged = cutover._pre_managed_control_stage_receipt(plan)
    Path(plan["selector_state"]).write_bytes(b"foreign-mutation\n")

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="selector_state changed before restore",
    ):
        cutover._restore_pre_managed_control_state(plan, captured, staged)

    assert Path(plan["selector_state"]).read_bytes() == b"foreign-mutation\n"


def test_pre_managed_control_restore_rolls_back_exact_owned_selector_activation(
    tmp_path,
):
    plan = _pre_managed_control_plan(tmp_path)
    control_root = Path(plan["selector_state"]).parent
    last_good = _managed_release(control_root, "last-good")
    candidate = _managed_release(control_root, "candidate")
    plan.update(
        {
            "expected_candidate_identity": {"build_id": "candidate"},
            "last_good_identity": {"build_id": "last-good"},
        }
    )
    selector.initialize_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
        release_root=last_good["release_root"],
        bootstrap_build_id="last-good",
        bootstrap_record=last_good["record"],
    )
    staged_state = selector.update_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
        expected_generation=0,
        transition=lambda current: selector.stage_candidate(
            current,
            "candidate",
            candidate["record"],
            transaction_id=plan["transaction_id"],
        ),
    )
    Path(plan["managed_plist"]).write_bytes(b"before-plist\n")
    Path(plan["managed_plist"]).chmod(0o600)
    captured = cutover._capture_pre_managed_control_state(plan)
    staged = cutover._pre_managed_control_stage_receipt(plan)
    activated_state = selector.update_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
        expected_generation=staged_state["generation"],
        transition=selector.activate_candidate,
    )

    restored = cutover._restore_pre_managed_control_state(
        plan,
        captured,
        staged,
    )

    current = selector.read_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
    )
    assert restored["controls"]["selector_state"]["status"] == (
        "rolled-back-owned-activation"
    )
    assert current["generation"] == activated_state["generation"] + 1
    assert current["current"] == current["last_good"] == "last-good"
    assert current["candidate"] is None
    assert current["pending_transaction_id"] is None
    assert current["releases"]["candidate"] == candidate["record"]
    resumed = cutover._restore_pre_managed_control_state(
        plan,
        captured,
        staged,
    )
    assert resumed["controls"]["selector_state"]["status"] == (
        "already-rolled-back-owned-activation"
    )


def test_candidate_startup_generation_requires_the_post_activation_generation():
    plan = {
        "transaction_id": "startup-generation-transaction-0001",
        "expected_candidate_identity": {
            "build_id": "candidate",
            "selector_generation": 8,
        },
        "last_good_identity": {"build_id": "last-good"},
    }
    staged = {
        "generation": 7,
        "current": "last-good",
        "candidate": "candidate",
        "pending_transaction_id": plan["transaction_id"],
        "last_good": "last-good",
    }

    assert cutover._attest_candidate_startup_generation(plan, staged) == {
        "staged_generation": 7,
        "startup_generation": 8,
    }
    plan["expected_candidate_identity"]["selector_generation"] = 7
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="candidate selector generation is not the post-activation generation",
    ):
        cutover._attest_candidate_startup_generation(plan, staged)


def test_pre_managed_control_resume_restages_only_rollback_owned_absence(
    monkeypatch,
    tmp_path,
):
    plan = _pre_managed_control_plan(tmp_path)
    plan.update(
        {
            "selector_path": str(tmp_path / "control" / "selector.py"),
            "bootstrap_rollback_plist": str(
                tmp_path / "rollback" / "webui.plist"
            ),
            "launchd_label": "com.example.hermes-webui",
            "managed_interpreter": str(tmp_path / "runtime" / "python"),
        }
    )
    captured = cutover._capture_pre_managed_control_state(plan)
    prepared = {
        "pre_managed_controls": captured,
        "legacy": {
            "program_arguments": [
                str(tmp_path / "legacy" / "python"),
                str(tmp_path / "legacy" / "bootstrap.py"),
            ],
            "routing_environment": {},
        },
    }
    selector_payload = b'{"candidate":"owned"}\n'
    lock_payload = b"owned-lock\n"
    transformed = {
        "Label": "com.example.hermes-webui",
        "ProgramArguments": [str(tmp_path / "runtime" / "python")],
    }

    def prepare_selector(_plan):
        for key, payload in (
            ("selector_state", selector_payload),
            ("selector_lock", lock_payload),
        ):
            path = Path(plan[key])
            path.write_bytes(payload)
            path.chmod(0o600)
        return {"candidate": "owned"}

    monkeypatch.setattr(
        cutover,
        "_prepare_bootstrap_selector",
        prepare_selector,
    )
    monkeypatch.setattr(
        cutover,
        "_read_plist",
        lambda _path: {"Label": "legacy"},
    )
    monkeypatch.setattr(
        cutover,
        "transform_launchd_target",
        lambda *_args, **_kwargs: transformed,
    )
    expected = cutover._stage_pre_managed_controls(plan, prepared)
    expected_plist = plistlib.dumps(
        transformed,
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )
    for key in ("selector_state", "selector_lock", "managed_plist"):
        Path(plan[key]).unlink()

    observed = cutover._adopt_or_restage_pre_managed_controls(
        plan,
        prepared,
        expected,
    )

    assert observed == expected
    assert Path(plan["selector_state"]).read_bytes() == selector_payload
    assert Path(plan["selector_lock"]).read_bytes() == lock_payload
    assert Path(plan["managed_plist"]).read_bytes() == expected_plist

    Path(plan["managed_plist"]).unlink()
    Path(plan["selector_state"]).write_bytes(b"foreign-selector\n")
    Path(plan["selector_state"]).chmod(0o600)
    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="pre-managed selector_state changed",
    ):
        cutover._adopt_or_restage_pre_managed_controls(
            plan,
            prepared,
            expected,
        )


def test_pre_managed_control_resume_adopts_durably_promoted_selector(
    tmp_path,
):
    plan = _pre_managed_control_plan(tmp_path)
    control_root = Path(plan["selector_state"]).parent
    last_good = _managed_release(control_root, "last-good")
    candidate = _managed_release(control_root, "candidate")
    plan.update(
        {
            "expected_candidate_identity": {
                "build_id": "candidate",
                "selector_generation": 2,
            },
            "last_good_identity": {"build_id": "last-good"},
        }
    )
    selector.initialize_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
        release_root=last_good["release_root"],
        bootstrap_build_id="last-good",
        bootstrap_record=last_good["record"],
    )
    Path(plan["managed_plist"]).write_bytes(b"managed-plist\n")
    Path(plan["managed_plist"]).chmod(0o600)
    captured = cutover._capture_pre_managed_control_state(plan)
    staged_state = selector.update_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
        expected_generation=0,
        transition=lambda current: selector.stage_candidate(
            current,
            "candidate",
            candidate["record"],
            transaction_id=plan["transaction_id"],
        ),
    )
    expected = cutover._pre_managed_control_stage_receipt(plan)
    cutover.initialize_transaction_journal(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
        expected_candidate_identity=plan["expected_candidate_identity"],
        rollback_receipt={
            "build_id": "last-good",
            "plist_sha256": "a" * 64,
            "state_snapshot_id": "pre-managed-selector-snapshot",
            "state_snapshot_sha256": "b" * 64,
        },
    )
    for phase in (
        "staged",
        "plist_installed",
        "old_fenced",
        "old_committed",
    ):
        cutover.record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase=phase,
            receipt={"status": "recorded"},
        )
    activated_state = selector.update_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
        expected_generation=staged_state["generation"],
        transition=selector.activate_candidate,
    )
    cutover.record_transaction_phase(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
        phase="selection_activated",
        receipt={"selection": activated_state},
    )
    prepared = {"pre_managed_controls": captured}

    activated = cutover._adopt_or_restage_pre_managed_controls(
        plan,
        prepared,
        expected,
    )

    assert activated["status"] == "adopted-owned-forward-transition"
    assert activated["selector_transition"]["transition"] == "activated"
    assert activated["selector_transition"]["selection"] == activated_state
    assert selector.read_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
    ) == activated_state

    promoted_state = selector.update_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
        expected_generation=activated_state["generation"],
        transition=selector.promote_candidate,
    )
    for phase in (
        "old_stopped",
        "replacement_proved",
        "candidate_fenced_health_proved",
        "pair_ready",
        "pair_commit_intent",
        "promoted",
    ):
        cutover.record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase=phase,
            receipt=(
                {
                    "promotion": {
                        "selector_and_cli": {"selector": promoted_state},
                    }
                }
                if phase == "promoted"
                else {"status": "recorded"}
            ),
        )

    observed = cutover._adopt_or_restage_pre_managed_controls(
        plan,
        prepared,
        expected,
    )

    assert observed["status"] == "adopted-owned-forward-transition"
    assert observed["selector_transition"]["transition"] == "promoted"
    assert observed["selector_transition"]["selection"] == promoted_state
    assert selector.read_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
    ) == promoted_state

    foreign_state = copy.deepcopy(promoted_state)
    foreign_state["generation"] += 1
    Path(plan["selector_state"]).write_text(
        json.dumps(foreign_state, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    Path(plan["selector_state"]).chmod(0o600)
    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="pre-managed selector_state changed on bootstrap resume",
    ):
        cutover._adopt_or_restage_pre_managed_controls(
            plan,
            prepared,
            expected,
        )


def test_reconcile_cutover_journal_accepts_exact_durable_promotion(
    monkeypatch,
    tmp_path,
):
    candidate = {
        "build_id": "candidate",
        "commit": "d" * 40,
        "tree": "e" * 40,
        "manifest_sha256": "f" * 64,
        "selector_generation": 2,
        "startup_fenced": True,
        "startup_transaction_id": "reconcile-promoted-transaction-0001",
    }
    plan = {
        "transaction_id": candidate["startup_transaction_id"],
        "expected_candidate_identity": candidate,
        "last_good_identity": {"build_id": "last-good"},
        "selector_state": str(tmp_path / "selector-state.json"),
        "selector_lock": str(tmp_path / "selector-state.lock"),
        "managed_plist": str(tmp_path / "managed.plist"),
        "bootstrap_rollback_plist": str(tmp_path / "rollback.plist"),
        "installed_plist": str(tmp_path / "installed.plist"),
        "snapshot_manifest": str(tmp_path / "snapshot.json"),
        "transaction_journal": str(tmp_path / "transaction.json"),
        "cli_old_target": str(tmp_path / "legacy-hermes"),
    }
    promoted_state = {
        "version": 2,
        "generation": 3,
        "release_root": str(tmp_path / "releases"),
        "current": "candidate",
        "last_good": "candidate",
        "candidate": None,
        "pending_transaction_id": None,
        "bootstrap_fallback": "last-good",
        "releases": {
            "last-good": {
                "commit": "a" * 40,
                "tree": "b" * 40,
                "manifest_sha256": "c" * 64,
            },
            "candidate": {
                "commit": "d" * 40,
                "tree": "e" * 40,
                "manifest_sha256": "f" * 64,
            },
        },
    }
    journal = {
        "expected_candidate_identity": candidate,
        "rollback_receipt": {},
        "phases": {
            "staged": {"status": "recorded"},
            "plist_installed": {"status": "recorded"},
            "old_fenced": {"status": "recorded"},
            "old_committed": {"status": "recorded"},
            "selection_activated": {"status": "recorded"},
            "old_stopped": {"status": "recorded"},
            "replacement_proved": {"status": "recorded"},
            "candidate_fenced_health_proved": {"status": "recorded"},
            "pair_ready": {"status": "recorded"},
            "pair_commit_intent": {"status": "recorded"},
            "promoted": {
                "promotion": {
                    "selector_and_cli": {"selector": promoted_state},
                }
            },
        },
    }
    monkeypatch.setattr(
        cutover,
        "_selector_state_attestation",
        lambda _plan: {
            "status": "verified",
            "transaction_id": plan["transaction_id"],
            "generation": promoted_state["generation"],
            "current": promoted_state["current"],
            "candidate": None,
            "pending_transaction_id": None,
            "last_good": promoted_state["last_good"],
        },
    )
    monkeypatch.setattr(
        cutover.release_selector,
        "read_selector_state",
        lambda *_args, **_kwargs: copy.deepcopy(promoted_state),
    )
    monkeypatch.setattr(
        cutover,
        "sha256_file",
        lambda path: (
            "1" * 64
            if Path(path) == Path(plan["managed_plist"])
            else "2" * 64
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_installed_plist_attestation",
        lambda _plan: {"plist_sha256": "1" * 64},
    )
    monkeypatch.setattr(
        cutover,
        "_read_json_object",
        lambda *_args, **_kwargs: {"snapshot_id": "snapshot"},
    )
    monkeypatch.setattr(cutover, "verify_state_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cutover,
        "read_transaction_journal",
        lambda *_args, **_kwargs: copy.deepcopy(journal),
    )

    assert cutover._reconcile_cutover_journal(plan) == journal

    journal["phases"].pop("pair_commit_intent")
    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="promoted selector does not match durable transaction",
    ):
        cutover._reconcile_cutover_journal(plan)

    journal["phases"]["pair_commit_intent"] = {"status": "recorded"}
    monkeypatch.setattr(
        cutover.release_selector,
        "read_selector_state",
        lambda *_args, **_kwargs: {
            **copy.deepcopy(promoted_state),
            "generation": promoted_state["generation"] + 1,
        },
    )
    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="promoted selector changed during journal reconciliation",
    ):
        cutover._reconcile_cutover_journal(plan)

    monkeypatch.setattr(
        cutover.release_selector,
        "read_selector_state",
        lambda *_args, **_kwargs: copy.deepcopy(promoted_state),
    )
    journal["expected_candidate_identity"] = {
        **candidate,
        "tree": "0" * 40,
    }
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="transaction journal candidate identity mismatch",
    ):
        cutover._reconcile_cutover_journal(plan)

    journal["expected_candidate_identity"] = candidate

    def unreadable_journal(*_args, **_kwargs):
        raise cutover.ReleaseBuildError("transaction journal is unreadable")

    monkeypatch.setattr(
        cutover,
        "read_transaction_journal",
        unreadable_journal,
    )
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="promoted selector durable journal is unavailable",
    ):
        cutover._reconcile_cutover_journal(plan)

    def journal_io_error(*_args, **_kwargs):
        raise OSError("journal lock unavailable")

    monkeypatch.setattr(
        cutover,
        "read_transaction_journal",
        journal_io_error,
    )
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="promoted selector durable journal is unavailable",
    ):
        cutover._reconcile_cutover_journal(plan)

    monkeypatch.setattr(
        cutover,
        "read_transaction_journal",
        lambda *_args, **_kwargs: copy.deepcopy(journal),
    )
    plan["last_good_identity"] = {"build_id": "candidate"}
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="cutover selector identities are invalid",
    ):
        cutover._reconcile_cutover_journal(plan)


@pytest.mark.parametrize("selector_phase", ["staged", "activated", "last-good"])
def test_reconcile_cutover_journal_preserves_pre_promotion_states(
    monkeypatch,
    tmp_path,
    selector_phase,
):
    transaction_id = "reconcile-pre-promotion-transaction-0001"
    candidate = {
        "build_id": "candidate",
        "startup_fenced": True,
        "startup_transaction_id": transaction_id,
    }
    plan = {
        "transaction_id": transaction_id,
        "expected_candidate_identity": candidate,
        "last_good_identity": {"build_id": "last-good"},
        "managed_plist": str(tmp_path / "managed.plist"),
        "bootstrap_rollback_plist": str(tmp_path / "rollback.plist"),
        "installed_plist": str(tmp_path / "installed.plist"),
        "snapshot_manifest": str(tmp_path / "snapshot.json"),
        "transaction_journal": str(tmp_path / "transaction.json"),
        "cli_old_target": str(tmp_path / "legacy-hermes"),
    }
    states = {
        "staged": {
            "generation": 1,
            "current": "last-good",
            "candidate": "candidate",
            "pending_transaction_id": transaction_id,
            "last_good": "last-good",
        },
        "activated": {
            "generation": 2,
            "current": "candidate",
            "candidate": "candidate",
            "pending_transaction_id": transaction_id,
            "last_good": "last-good",
        },
        "last-good": {
            "generation": 0,
            "current": "last-good",
            "candidate": None,
            "pending_transaction_id": None,
            "last_good": "last-good",
        },
    }
    selector_attestation = {
        "status": "verified",
        "transaction_id": transaction_id,
        **states[selector_phase],
    }
    journal = {
        "expected_candidate_identity": candidate,
        "rollback_receipt": {},
        "phases": {
            "staged": {"status": "recorded"},
            "plist_installed": {"status": "recorded"},
        },
    }
    if selector_phase == "activated":
        journal["phases"]["old_committed"] = {
            "identity": {
                "pid": 123,
                "pid_start_token": "old-start",
            }
        }
    active_journal = copy.deepcopy(journal)
    monkeypatch.setattr(
        cutover,
        "_selector_state_attestation",
        lambda _plan: copy.deepcopy(selector_attestation),
    )
    monkeypatch.setattr(
        cutover,
        "sha256_file",
        lambda path: (
            "1" * 64
            if Path(path) == Path(plan["managed_plist"])
            else "2" * 64
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_installed_plist_attestation",
        lambda _plan: {"plist_sha256": "1" * 64},
    )
    monkeypatch.setattr(
        cutover,
        "_read_json_object",
        lambda *_args, **_kwargs: {"snapshot_id": "snapshot"},
    )
    monkeypatch.setattr(cutover, "verify_state_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cutover,
        "read_transaction_journal",
        lambda *_args, **_kwargs: copy.deepcopy(active_journal),
    )
    monkeypatch.setattr(
        cutover,
        "_pid_start_token",
        lambda _pid: "old-start",
    )
    monkeypatch.setattr(cutover, "_launchd_pid", lambda _plan: 123)

    def record_phase(*_args, phase, receipt, **_kwargs):
        active_journal["phases"][phase] = receipt
        return copy.deepcopy(active_journal)

    monkeypatch.setattr(cutover, "record_transaction_phase", record_phase)

    reconciled = cutover._reconcile_cutover_journal(plan)
    if selector_phase == "activated":
        assert reconciled["phases"]["selection_activated"] == {
            "selection": {
                "generation": selector_attestation["generation"],
                "external_state_reconciled": True,
            }
        }
    else:
        assert reconciled == journal


def _process_checkpoint_plan(tmp_path: Path) -> tuple[dict, Path]:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    process_store = hermes_home / "process_notifications.json"
    process_store.write_text("{}\n", encoding="utf-8")
    process_store.chmod(0o600)
    cron = hermes_home / "cron"
    cron.mkdir(mode=0o700)
    tick_lock = cron / ".tick.lock"
    tick_lock.write_bytes(b"")
    tick_lock.chmod(0o600)
    return (
        {
            "synthetic_process_notifications_path": str(process_store),
            "gateway_listener_port": 8642,
            "timeout_seconds": 0.2,
            "interval_seconds": 0.001,
        },
        hermes_home / "processes.json",
    )


def test_gateway_process_checkpoint_requires_exact_private_empty_list(tmp_path):
    plan, checkpoint = _process_checkpoint_plan(tmp_path)
    checkpoint.write_text("[]\n", encoding="utf-8")
    checkpoint.chmod(0o600)

    receipt = cutover._legacy_process_checkpoint_receipt(plan)

    assert receipt["status"] == "verified"
    assert receipt["path"] == str(checkpoint)
    assert receipt["active_records"] == 0
    assert receipt["mode"] == 0o600
    assert receipt["nlink"] == 1
    assert receipt["sha256"] == hashlib.sha256(b"[]\n").hexdigest()


@pytest.mark.parametrize(
    "mutation",
    [
        "absent",
        "symlink",
        "hardlink",
        "permissive",
        "non-list",
        "nonempty",
    ],
)
def test_gateway_process_checkpoint_unsafe_or_nonempty_never_authorizes_stop(
    tmp_path,
    mutation,
):
    plan, checkpoint = _process_checkpoint_plan(tmp_path)
    if mutation == "symlink":
        target = checkpoint.with_name("processes-target.json")
        target.write_text("[]\n", encoding="utf-8")
        target.chmod(0o600)
        checkpoint.symlink_to(target)
    elif mutation == "hardlink":
        target = checkpoint.with_name("processes-target.json")
        target.write_text("[]\n", encoding="utf-8")
        target.chmod(0o600)
        os.link(target, checkpoint)
    elif mutation != "absent":
        checkpoint.write_text(
            '{"not":"a-list"}\n' if mutation == "non-list" else (
                '[{"pid":321}]\n' if mutation == "nonempty" else "[]\n"
            ),
            encoding="utf-8",
        )
        checkpoint.chmod(0o644 if mutation == "permissive" else 0o600)

    with pytest.raises(cutover.ReleaseBuildError, match="process checkpoint"):
        cutover._legacy_process_checkpoint_receipt(plan)


def test_gateway_process_checkpoint_same_inode_mutation_is_rejected(
    tmp_path,
    monkeypatch,
):
    plan, checkpoint = _process_checkpoint_plan(tmp_path)
    checkpoint.write_text("[]\n", encoding="utf-8")
    checkpoint.chmod(0o600)
    original_read = cutover.os.read
    mutated = False

    def racing_read(descriptor, size):
        nonlocal mutated
        chunk = original_read(descriptor, size)
        if chunk and not mutated:
            mutated = True
            checkpoint.chmod(0o640)
        return chunk

    monkeypatch.setattr(cutover.os, "read", racing_read)

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="process checkpoint changed",
    ):
        cutover._legacy_process_checkpoint_receipt(plan)


def test_gateway_process_checkpoint_waits_naturally_without_signalling_workers(
    tmp_path,
    monkeypatch,
):
    plan, _checkpoint = _process_checkpoint_plan(tmp_path)
    prepared = {"gateway": {"pid": 41, "pid_start_token": "gateway-start"}}
    receipts = iter(
        [
            cutover.ReleaseBuildError(
                "gateway process checkpoint still has worker activity"
            ),
            {
                "status": "verified",
                "path": "processes.json",
                "active_records": 0,
            },
        ]
    )
    signals = []

    def checkpoint_receipt(_plan):
        value = next(receipts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(
        cutover,
        "_legacy_process_checkpoint_receipt",
        checkpoint_receipt,
    )
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: 41 if gateway else None,
    )
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")
    monkeypatch.setattr(cutover.os, "kill", lambda *args: signals.append(args))

    receipt = cutover._wait_for_legacy_process_checkpoint_empty(plan, prepared)

    assert receipt["active_records"] == 0
    assert signals == []


def test_process_retirement_barrier_releases_authority_during_gateway_exit(
    tmp_path,
    monkeypatch,
):
    plan, checkpoint = _process_checkpoint_plan(tmp_path)
    checkpoint.write_text("[]\n", encoding="utf-8")
    checkpoint.chmod(0o600)
    events = []
    admissions = iter(
        [
            {
                "kind": "admission",
                "handle": object(),
                "receipt": {"path": "admission.lock"},
            }
        ]
    )
    authorities = iter(
        [
            {
                "kind": "authority",
                "handle": object(),
                "receipt": {"path": "authority.lock", "epoch": 1},
            },
            {
                "kind": "authority",
                "handle": object(),
                "receipt": {"path": "authority.lock", "epoch": 2},
            },
        ]
    )

    def acquire(_plan, *, kind):
        events.append(f"acquire-{kind}")
        return next(admissions if kind == "admission" else authorities)

    def release(_plan, held):
        events.append(f"release-{held['kind']}")
        return {"status": "released", "kind": held["kind"]}

    monkeypatch.setattr(cutover, "_acquire_process_registry_lock", acquire)
    monkeypatch.setattr(cutover, "_release_process_registry_lock", release)
    monkeypatch.setattr(
        cutover,
        "_legacy_process_checkpoint_receipt",
        lambda _plan: events.append("checkpoint")
        or {
            "status": "verified",
            "path": str(checkpoint),
            "active_records": 0,
            "sha256": hashlib.sha256(b"[]\n").hexdigest(),
        },
    )

    receipt = cutover._run_process_registry_retirement_barrier(
        plan,
        stop_gateway=lambda: events.append("stop-and-wait")
        or {"status": "stopped"},
    )

    assert events == [
        "acquire-admission",
        "acquire-authority",
        "checkpoint",
        "release-authority",
        "stop-and-wait",
        "acquire-authority",
        "checkpoint",
        "release-authority",
        "release-admission",
    ]
    assert receipt["status"] == "retired-at-zero"
    assert receipt["pre_stop_checkpoint"]["active_records"] == 0
    assert receipt["post_exit_checkpoint"]["active_records"] == 0


def test_process_retirement_barrier_never_stops_on_nonzero_checkpoint(
    tmp_path,
    monkeypatch,
):
    plan, _checkpoint = _process_checkpoint_plan(tmp_path)
    events = []

    def acquire(_plan, *, kind):
        events.append(f"acquire-{kind}")
        return {"kind": kind, "handle": object(), "receipt": {"kind": kind}}

    def release(_plan, held):
        events.append(f"release-{held['kind']}")
        return {"status": "released", "kind": held["kind"]}

    monkeypatch.setattr(cutover, "_acquire_process_registry_lock", acquire)
    monkeypatch.setattr(cutover, "_release_process_registry_lock", release)
    monkeypatch.setattr(
        cutover,
        "_legacy_process_checkpoint_receipt",
        lambda _plan: (_ for _ in ()).throw(
            cutover.ReleaseBuildError(
                "gateway process checkpoint still has worker activity"
            )
        ),
    )

    with pytest.raises(cutover.ReleaseBuildError, match="worker activity"):
        cutover._run_process_registry_retirement_barrier(
            plan,
            stop_gateway=lambda: events.append("unsafe-stop"),
        )

    assert events == [
        "acquire-admission",
        "acquire-authority",
        "release-authority",
        "release-admission",
    ]


def test_gateway_stop_rechecks_checkpoint_before_bootout(tmp_path, monkeypatch):
    plan, _checkpoint = _process_checkpoint_plan(tmp_path)
    plan["transaction_id"] = "checkpoint-stop-transaction-000001"
    prepared = {"gateway": {"pid": 41, "pid_start_token": "gateway-start"}}
    intent = {
        "planned_stop": {
            "path": str(cutover._legacy_gateway_planned_stop_path(plan)),
            "payload": {"release_transaction_id": plan["transaction_id"]},
        },
        "launchd_restart_control": {
            "status": "prepared",
            "initial": {
                "target": "gui/501/ai.hermes.gateway",
                "disabled": False,
            },
            "restore_semantics": "enabled",
        },
        "clean_shutdown_baseline": {
            "exists": False,
            "mtime_ns": None,
        },
    }
    bootouts = []
    marker_writes = []
    checkpoint_reads = iter(
        [
            {
                "status": "verified",
                "path": "processes.json",
                "active_records": 0,
            },
            cutover.ReleaseBuildError(
                "gateway process checkpoint still has worker activity"
            ),
        ]
    )

    def checkpoint_receipt(_plan):
        value = next(checkpoint_reads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(
        cutover,
        "_legacy_process_checkpoint_receipt",
        checkpoint_receipt,
    )
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _identity: True)
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: 41 if gateway else None,
    )
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(
        cutover,
        "_pid_start_token",
        lambda _pid: "gateway-start",
    )
    monkeypatch.setattr(
        cutover,
        "_set_launchd_service_disabled",
        lambda *_args, disabled, **_kwargs: {
            "status": "disabled" if disabled else "enabled"
        },
    )
    monkeypatch.setattr(
        cutover,
        "_write_exact_private_json",
        lambda *args, **kwargs: marker_writes.append((args, kwargs)) or {},
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *args, **kwargs: bootouts.append((args, kwargs)),
    )
    process_state = {"value": "S"}

    def signal_process(_pid, sent_signal):
        process_state["value"] = "T" if sent_signal == signal.SIGSTOP else "S"

    monkeypatch.setattr(cutover.os, "kill", signal_process)
    monkeypatch.setattr(
        cutover,
        "_ps_value",
        lambda _pid, _field: process_state["value"],
    )

    with pytest.raises(cutover.ReleaseBuildError, match="process checkpoint"):
        cutover._gracefully_stop_legacy_gateway(plan, prepared, intent)
    cutover._release_legacy_cron_tick_lock(plan)

    assert bootouts == []
    assert marker_writes == []


def test_gateway_stop_rechecks_exact_owner_after_second_checkpoint(
    tmp_path,
    monkeypatch,
):
    plan, _checkpoint = _process_checkpoint_plan(tmp_path)
    plan["transaction_id"] = "checkpoint-owner-race-transaction-000001"
    prepared = {"gateway": {"pid": 41, "pid_start_token": "gateway-start"}}
    intent = {
        "planned_stop": {
            "path": str(cutover._legacy_gateway_planned_stop_path(plan)),
            "payload": {"release_transaction_id": plan["transaction_id"]},
        },
        "launchd_restart_control": {
            "status": "prepared",
            "initial": {
                "target": "gui/501/ai.hermes.gateway",
                "disabled": False,
            },
            "restore_semantics": "enabled",
        },
        "clean_shutdown_baseline": {
            "exists": False,
            "mtime_ns": None,
        },
    }
    bootouts = []
    job_pids = iter([41, 99])
    monkeypatch.setattr(
        cutover,
        "_legacy_process_checkpoint_receipt",
        lambda _plan: {
            "status": "verified",
            "path": "processes.json",
            "active_records": 0,
        },
    )
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _identity: True)
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: next(job_pids) if gateway else None,
    )
    monkeypatch.setattr(
        cutover,
        "_set_launchd_service_disabled",
        lambda *_args, disabled, **_kwargs: {
            "status": "disabled" if disabled else "enabled"
        },
    )
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")
    monkeypatch.setattr(
        cutover,
        "_write_exact_private_json",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *args, **kwargs: bootouts.append((args, kwargs)),
    )
    process_state = {"value": "S"}

    def signal_process(_pid, sent_signal):
        process_state["value"] = "T" if sent_signal == signal.SIGSTOP else "S"

    monkeypatch.setattr(cutover.os, "kill", signal_process)
    monkeypatch.setattr(
        cutover,
        "_ps_value",
        lambda _pid, _field: process_state["value"],
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="changed immediately before graceful stop",
    ):
        cutover._gracefully_stop_legacy_gateway(plan, prepared, intent)
    cutover._release_legacy_cron_tick_lock(plan)

    assert bootouts == []


@pytest.mark.parametrize(
    "drain_line",
    [
        (
            "Shutdown phase: drain done "
            "(timed_out=False, active_at_start=0, active_now=0, "
            "cron_at_start=0, cron_now=0)"
        ),
        (
            "Shutdown phase: drain done at +0.01s "
            "(drain took 0.00s, timed_out=False, active_at_start=3, "
            "active_now=0, cron_at_start=7, cron_now=0)"
        ),
    ],
)
def test_legacy_gateway_shutdown_log_accepts_strict_clean_formats(drain_line):
    combined = "\n".join(
        [
            drain_line,
            "[Api_Server] API server stopped",
            "Gateway stopped (total teardown 0.30s)",
        ]
    )

    receipt = cutover._parse_legacy_gateway_shutdown_log(combined)

    assert receipt["timed_out"] is False
    assert receipt["active_now"] == 0
    assert receipt["cron_now"] == 0
    assert receipt["active_at_start"] in {0, 3}
    assert receipt["cron_at_start"] in {0, 7}


@pytest.mark.parametrize(
    "drain_fields",
    [
        "timed_out=True, active_at_start=0, active_now=0, "
        "cron_at_start=0, cron_now=0",
        "timed_out=False, active_at_start=0, active_now=1, "
        "cron_at_start=0, cron_now=0",
        "timed_out=False, active_at_start=0, active_now=0, "
        "cron_at_start=0, cron_now=1",
    ],
)
def test_legacy_gateway_shutdown_log_rejects_timeout_or_remaining_work(
    drain_fields,
):
    combined = "\n".join(
        [
            f"Shutdown phase: drain done at +0.01s "
            f"(drain took 0.00s, {drain_fields})",
            "[Api_Server] API server stopped",
            "Gateway stopped (total teardown 0.30s)",
        ]
    )

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="zero-work clean stop",
    ):
        cutover._parse_legacy_gateway_shutdown_log(combined)


def test_legacy_gateway_shutdown_log_preserves_natural_drain_counts():
    combined = "\n".join(
        [
            "Shutdown phase: drain done at +12s "
            "(drain took 11.5s, timed_out=False, active_at_start=3, "
            "active_now=0, cron_at_start=7, cron_now=0)",
            "[Api_Server] API server stopped",
            "Gateway stopped (total teardown 12.1s)",
        ]
    )

    assert cutover._parse_legacy_gateway_shutdown_log(combined) == {
        "timed_out": False,
        "active_at_start": 3,
        "active_now": 0,
        "cron_at_start": 7,
        "cron_now": 0,
    }


@pytest.mark.parametrize(
    "terminal_line",
    [
        "",
        "[Api_Server] API server stopped",
        "Gateway stopped (total teardown 0.30s)",
        "Gateway drain timed out\n"
        "[Api_Server] API server stopped\n"
        "Gateway stopped (total teardown 0.30s)",
        "Skipping .clean_shutdown marker\n"
        "[Api_Server] API server stopped\n"
        "Gateway stopped (total teardown 0.30s)",
    ],
)
def test_legacy_gateway_shutdown_log_requires_unambiguous_clean_terminal_receipts(
    terminal_line,
):
    combined = "\n".join(
        [
            "Shutdown phase: drain done "
            "(timed_out=False, active_at_start=0, active_now=0, "
            "cron_at_start=0, cron_now=0)",
            terminal_line,
        ]
    )

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="zero-work clean stop",
    ):
        cutover._parse_legacy_gateway_shutdown_log(combined)


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_legacy_gateway_status_reader_accepts_exact_owned_regular_file(
    tmp_path,
    mode,
):
    status_path = tmp_path / "gateway_state.json"
    payload = {
        "kind": "hermes-gateway",
        "pid": 41,
        "gateway_state": "draining",
        "active_agents": 0,
    }
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    status_path.chmod(mode)

    status, receipt = cutover._read_legacy_gateway_status(
        status_path,
        label="legacy gateway status",
    )

    assert status == payload
    assert receipt["path"] == str(status_path)
    assert receipt["mode"] == mode
    assert receipt["uid"] == os.getuid()
    assert receipt["nlink"] == 1
    assert receipt["size"] == status_path.stat().st_size
    assert receipt["sha256"] == _sha(status_path)


@pytest.mark.parametrize("unsafe_mode", [0o640, 0o666])
def test_legacy_gateway_status_reader_rejects_unsafe_mode(
    tmp_path,
    unsafe_mode,
):
    status_path = tmp_path / "gateway_state.json"
    status_path.write_text("{}", encoding="utf-8")
    status_path.chmod(unsafe_mode)

    with pytest.raises(cutover.ReleaseBuildError, match="unsafe"):
        cutover._read_legacy_gateway_status(
            status_path,
            label="legacy gateway status",
        )


def test_legacy_gateway_status_reader_rejects_symlink(tmp_path):
    target = tmp_path / "real-gateway-state.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)
    status_path = tmp_path / "gateway_state.json"
    status_path.symlink_to(target)

    with pytest.raises(cutover.ReleaseBuildError, match="unreadable|unsafe"):
        cutover._read_legacy_gateway_status(
            status_path,
            label="legacy gateway status",
        )


def test_legacy_gateway_status_reader_rejects_hardlink(tmp_path):
    status_path = tmp_path / "gateway_state.json"
    status_path.write_text("{}", encoding="utf-8")
    status_path.chmod(0o644)
    os.link(status_path, tmp_path / "gateway_state-copy.json")

    with pytest.raises(cutover.ReleaseBuildError, match="unsafe"):
        cutover._read_legacy_gateway_status(
            status_path,
            label="legacy gateway status",
        )


def test_legacy_gateway_status_reader_rejects_path_swap(tmp_path, monkeypatch):
    status_path = tmp_path / "gateway_state.json"
    replacement = tmp_path / "replacement.json"
    status_path.write_text('{"gateway_state":"draining"}', encoding="utf-8")
    replacement.write_text('{"gateway_state":"stopped"}', encoding="utf-8")
    status_path.chmod(0o644)
    replacement.chmod(0o644)
    original_fstat = os.fstat
    calls = 0

    def swap_after_read(descriptor):
        nonlocal calls
        result = original_fstat(descriptor)
        calls += 1
        if calls == 2:
            replacement.replace(status_path)
        return result

    monkeypatch.setattr(cutover.os, "fstat", swap_after_read)

    with pytest.raises(cutover.ReleaseBuildError, match="identity changed"):
        cutover._read_legacy_gateway_status(
            status_path,
            label="legacy gateway status",
        )


def test_legacy_gateway_status_reader_rejects_non_object_json(tmp_path):
    status_path = tmp_path / "gateway_state.json"
    status_path.write_text("[]", encoding="utf-8")
    status_path.chmod(0o644)

    with pytest.raises(cutover.ReleaseBuildError, match="JSON object"):
        cutover._read_legacy_gateway_status(
            status_path,
            label="legacy gateway status",
        )


def test_legacy_gateway_status_reader_requires_nofollow(tmp_path, monkeypatch):
    status_path = tmp_path / "gateway_state.json"
    status_path.write_text("{}", encoding="utf-8")
    status_path.chmod(0o644)
    monkeypatch.delattr(cutover.os, "O_NOFOLLOW")

    with pytest.raises(cutover.ReleaseBuildError, match="no-follow"):
        cutover._read_legacy_gateway_status(
            status_path,
            label="legacy gateway status",
        )


def test_legacy_gateway_drain_accepts_exact_old_status_and_empty_checkpoint(
    tmp_path,
    monkeypatch,
):
    plan, _checkpoint = _process_checkpoint_plan(tmp_path)
    plan.update(
        {
            "transaction_id": "legacy-drain-transaction-000000001",
            "gateway_listener_port": 8642,
            "interval_seconds": 0.001,
            "timeout_seconds": 0.1,
        }
    )
    marker = tmp_path / "hermes-home" / ".drain_request.json"
    intent = {
        "status_baseline": {"mtime_ns": 10},
        "marker": {
            "path": str(marker),
            "payload": {
                "release_transaction_id": plan["transaction_id"],
            },
            "sha256": "a" * 64,
        },
    }
    prepared = {
        "gateway": {"pid": 41, "pid_start_token": "gateway-start"}
    }
    status_path = tmp_path / "hermes-home" / "gateway_state.json"
    status_path.write_text(
        json.dumps(
            {
                "kind": "hermes-gateway",
                "pid": 41,
                "gateway_state": "draining",
                "active_agents": 0,
            }
        ),
        encoding="utf-8",
    )
    status_path.chmod(0o644)
    monkeypatch.setattr(
        cutover,
        "_write_legacy_gateway_drain_marker",
        lambda *_args: {"path": str(marker), "sha256": "a" * 64},
    )
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(
        cutover,
        "_pid_start_token",
        lambda _pid: "gateway-start",
    )
    monkeypatch.setattr(
        cutover,
        "_regular_file_baseline",
        lambda *_args, **_kwargs: {
            "exists": True,
            "mtime_ns": 11,
            "sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_legacy_gateway_health_with_drain",
        lambda _plan: {
            "status": "ok",
            "platform": "hermes-agent",
            "version": "0.18.2",
        },
    )
    monkeypatch.setattr(
        cutover,
        "_wait_for_legacy_process_checkpoint_empty",
        lambda *_args: {
            "status": "verified",
            "active_records": 0,
        },
    )

    receipt = cutover._wait_for_legacy_gateway_drain(
        plan,
        prepared,
        intent,
    )

    assert receipt["status"] == "verified"
    assert receipt["work"] == {"active_agents": 0}
    assert receipt["health_mode"] == "legacy-status-file"


def test_legacy_gateway_health_without_api_key_uses_public_receipt(
    tmp_path,
    monkeypatch,
):
    plist = tmp_path / "gateway.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hermes.gateway",
                "EnvironmentVariables": {"HERMES_HOME": str(tmp_path)},
            }
        )
    )
    plan = {
        "gateway_health_url": "http://127.0.0.1:8642/health",
        "gateway_rollback_plist": str(plist),
        "timeout_seconds": 0.1,
    }
    public = {
        "status": "ok",
        "platform": "hermes-agent",
        "version": "0.18.2",
    }
    calls = []
    monkeypatch.setattr(
        cutover,
        "_http_json",
        lambda request, **_kwargs: calls.append(request) or public,
    )

    assert cutover._legacy_gateway_health_with_drain(plan) == public
    assert calls == [plan["gateway_health_url"]]


def test_legacy_tick_lock_mtime_churn_is_recoverable_under_kernel_lock(
    tmp_path,
):
    plan, _checkpoint = _process_checkpoint_plan(tmp_path)
    tick_lock = (
        Path(plan["synthetic_process_notifications_path"]).parent
        / "cron"
        / ".tick.lock"
    )
    plan.update(
        {
            "transaction_id": "tick-lock-recovery-transaction-000001",
            "timeout_seconds": 0.1,
            "interval_seconds": 0.001,
        }
    )
    tick_lock.chmod(0o644)
    intent = cutover._legacy_cron_tick_lock_normalize_intent_receipt(plan)
    normalized = cutover._normalize_legacy_cron_tick_lock(plan, intent)
    held = cutover._acquire_legacy_cron_tick_lock(plan)
    before = tick_lock.stat()
    os.utime(
        tick_lock,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000),
    )
    try:
        restored = cutover._restore_legacy_cron_tick_lock(
            plan,
            intent,
            normalized,
        )
    finally:
        cutover._release_legacy_cron_tick_lock(
            plan,
            allow_restored_mode=True,
        )

    assert held["status"] == "held"
    assert restored["status"] == "restored"
    assert stat.S_IMODE(tick_lock.stat().st_mode) == 0o644


def _canonical_gateway_health_fixture(
    *,
    pair_gate_active: bool,
) -> tuple[dict, dict, dict]:
    transaction_id = "gateway-health-transaction-00000001"
    identity = {
        "build_id": "candidate-webui",
        "commit": "1" * 40,
        "tree": "2" * 40,
        "manifest_sha256": "3" * 64,
        "agent_source_commit": "4" * 40,
        "agent_source_tree": "5" * 40,
        "agent_source_manifest_sha256": "6" * 64,
        "runtime_manifest_sha256": "7" * 64,
        "selector_generation": 7,
    }
    plan = {
        "gateway_health_url": "http://127.0.0.1:8642/health",
        "gateway_listener_port": 8642,
        "gateway_launchd_label": "ai.hermes.gateway",
        "transaction_id": transaction_id,
        "timeout_seconds": 1,
    }
    release_pair_id = selector.release_pair_id(
        identity,
        selector_generation=7,
        transaction_id=transaction_id,
    )
    release = {
        "agent_commit": identity["agent_source_commit"],
        "agent_tree": identity["agent_source_tree"],
        "agent_manifest_sha256": identity["agent_source_manifest_sha256"],
        "runtime_manifest_sha256": identity["runtime_manifest_sha256"],
        "release_pair_id": release_pair_id,
        "webui_build_id": identity["build_id"],
        "webui_commit": identity["commit"],
        "webui_tree": identity["tree"],
        "webui_manifest_sha256": identity["manifest_sha256"],
        "selector_generation": "7",
        "release_transaction_id": transaction_id,
        "gateway_launchd_label": plan["gateway_launchd_label"],
    }
    pair_gate_fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "agent_pair_gate_receipts.v1.json"
    )
    pair_gate_fixture_bytes = pair_gate_fixture_path.read_bytes()
    assert hashlib.sha256(pair_gate_fixture_bytes).hexdigest() == (
        "1cfa3c6ee77874803bcc7871c1304387a448810038077647b2b807cb9819a1f5"
    )
    pair_gate_fixture = json.loads(pair_gate_fixture_bytes)
    assert (
        pair_gate_fixture["schema"]
        == "hermes.agent_pair_gate_receipts.fixture.v1"
    )
    pair_gate = copy.deepcopy(
        pair_gate_fixture["active" if pair_gate_active else "absent"]
    )
    if pair_gate_active:
        assert pair_gate["transaction_id"] == transaction_id
        assert pair_gate["agent"]["instance_epoch"] == release_pair_id
    expected_admission = (
        "rejecting_new_work"
        if pair_gate_active
        else "accepting_new_work"
    )
    work_fields = {
        "active_http_requests",
        "active_agent_turns",
        "active_delegations",
        "background_processes",
        "process_completion_queue_depth",
        "active_cron_jobs",
        "api_background_tasks",
        "running_kanban_workers",
        "gateway_background_tasks",
    }
    health = {
        "status": "ok",
        "platform": "hermes-agent",
        "version": "0.1.0",
        "release_identity": {
            "schema": "hermes.public_release_identity.v1",
            "verified": True,
            "release": release,
            "process": {
                "pid": 41,
                "start_token": "gateway-start",
                "start_token_status": "verified",
            },
        },
        "drain": {
            "schema": "hermes.gateway_drain.v1",
            "admission": {
                "state": expected_admission,
                "verified": True,
                "drain_requested": False,
                "pair_open_gate_active": pair_gate_active,
                "effective_rejection_requested": pair_gate_active,
            },
            "work": {name: 0 for name in work_fields},
            "work_status": {name: "verified" for name in work_fields},
            "quiescence": {
                "verified": True,
                "quiescent": pair_gate_active,
                "blockers": (
                    [] if pair_gate_active else ["admission_not_rejecting"]
                ),
            },
            "pair_open_gate": pair_gate,
            "cron_admission": {
                "schema": "hermes.cron_admission.v1",
                "verified": True,
                "accepting": not pair_gate_active,
                "gate_epoch": 7,
                "active_count": 0,
                "active_job_ids": [],
                "active_leases": [],
            },
        },
    }
    return plan, identity, health


@pytest.mark.parametrize("pair_gate_active", [True, False])
def test_gateway_health_accepts_canonical_agent_pair_gate_receipts(
    monkeypatch,
    pair_gate_active,
):
    plan, identity, health = _canonical_gateway_health_fixture(
        pair_gate_active=pair_gate_active,
    )
    monkeypatch.setattr(cutover, "_http_json", lambda *args, **kwargs: health)
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")

    receipt = cutover._gateway_health_receipt(
        plan,
        expected_identity=identity,
        expected_admission=(
            "rejecting_new_work"
            if pair_gate_active
            else "accepting_new_work"
        ),
        expected_pair_gate=(
            health["drain"]["pair_open_gate"]
            if pair_gate_active
            else "absent"
        ),
    )

    assert receipt["drain"] == health["drain"]


def test_gateway_health_accepts_generated_absent_pair_gate_expectation(
    monkeypatch,
):
    plan, identity, health = _canonical_gateway_health_fixture(
        pair_gate_active=False,
    )
    expected_absent = cutover._expected_agent_pair_gate_receipt(
        {},
        active=False,
    )
    assert expected_absent == health["drain"]["pair_open_gate"]
    monkeypatch.setattr(cutover, "_http_json", lambda *args, **kwargs: health)
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")

    receipt = cutover._gateway_health_receipt(
        plan,
        expected_identity=identity,
        expected_admission="accepting_new_work",
        expected_pair_gate=expected_absent,
    )

    assert receipt["drain"] == health["drain"]


def test_gateway_health_attests_last_good_originating_transaction(monkeypatch):
    plan, identity, health = _canonical_gateway_health_fixture(
        pair_gate_active=False,
    )
    originating_transaction = plan["transaction_id"]
    identity["startup_transaction_id"] = originating_transaction
    plan["transaction_id"] = "gateway-health-next-transaction-0000001"
    monkeypatch.setattr(cutover, "_http_json", lambda *args, **kwargs: health)
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")

    receipt = cutover._gateway_health_receipt(
        plan,
        expected_identity=identity,
        expected_admission="accepting_new_work",
        expected_pair_gate="absent",
    )

    assert receipt["release_identity"]["release"][
        "release_transaction_id"
    ] == originating_transaction


def test_gateway_health_rejects_changed_absent_pair_gate_expectation(
    monkeypatch,
):
    plan, identity, health = _canonical_gateway_health_fixture(
        pair_gate_active=False,
    )
    expected_absent = cutover._expected_agent_pair_gate_receipt(
        {},
        active=False,
    )
    expected_absent["verified"] = False
    monkeypatch.setattr(cutover, "_http_json", lambda *args, **kwargs: health)
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="absent pair-open gate receipt changed",
    ):
        cutover._gateway_health_receipt(
            plan,
            expected_identity=identity,
            expected_admission="accepting_new_work",
            expected_pair_gate=expected_absent,
        )


@pytest.mark.parametrize("active", [None, 0, "false"])
def test_gateway_health_rejects_inexact_expected_pair_gate_state(
    monkeypatch,
    active,
):
    plan, identity, health = _canonical_gateway_health_fixture(
        pair_gate_active=False,
    )
    monkeypatch.setattr(cutover, "_http_json", lambda *args, **kwargs: health)
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")

    with pytest.raises(
        ValueError,
        match="expected pair-gate receipt has no exact state",
    ):
        cutover._gateway_health_receipt(
            plan,
            expected_identity=identity,
            expected_admission="accepting_new_work",
            expected_pair_gate={"active": active},
        )


def test_gateway_health_rejects_incomplete_work_status_schema(monkeypatch):
    plan, identity, health = _canonical_gateway_health_fixture(
        pair_gate_active=True,
    )
    health["drain"]["work_status"].pop("running_kanban_workers")
    monkeypatch.setattr(cutover, "_http_json", lambda *args, **kwargs: health)
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="drain receipt is not quiescent",
    ):
        cutover._gateway_health_receipt(
            plan,
            expected_identity=identity,
            expected_admission="rejecting_new_work",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verified", False),
        ("accepting", True),
        ("gate_epoch", True),
        ("active_count", 1),
        ("active_job_ids", ["job-1"]),
        (
            "active_leases",
            [
                {
                    "job_id": "job-1",
                    "source": "cron",
                    "pid": 42,
                    "gate_epoch": 7,
                    "admitted_at": "2026-07-23T00:00:00+00:00",
                }
            ],
        ),
    ],
)
def test_gateway_health_rejects_invalid_cron_admission_receipt(
    monkeypatch,
    field,
    value,
):
    plan, identity, health = _canonical_gateway_health_fixture(
        pair_gate_active=True,
    )
    health["drain"]["cron_admission"][field] = value
    monkeypatch.setattr(cutover, "_http_json", lambda *args, **kwargs: health)
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="drain receipt is not quiescent",
    ):
        cutover._gateway_health_receipt(
            plan,
            expected_identity=identity,
            expected_admission="rejecting_new_work",
            expected_pair_gate=health["drain"]["pair_open_gate"],
        )


def test_gateway_health_rejects_incomplete_cron_admission_schema(monkeypatch):
    plan, identity, health = _canonical_gateway_health_fixture(
        pair_gate_active=True,
    )
    health["drain"]["cron_admission"].pop("active_leases")
    monkeypatch.setattr(cutover, "_http_json", lambda *args, **kwargs: health)
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="drain receipt is not quiescent",
    ):
        cutover._gateway_health_receipt(
            plan,
            expected_identity=identity,
            expected_admission="rejecting_new_work",
            expected_pair_gate=health["drain"]["pair_open_gate"],
        )


def test_bootstrap_rollback_context_uses_exact_durable_legacy_receipts(
    monkeypatch,
):
    prepared = {
        "legacy": {"pid": 41, "pid_start_token": "webui-start"},
        "gateway": {"pid": 42, "pid_start_token": "gateway-start"},
    }
    drain_intent = {
        "status": "prepared",
        "marker": {
            "path": "/tmp/.drain_request.json",
            "payload": {
                "release_transaction_id": "bootstrap-transaction-000001",
            },
            "sha256": "d" * 64,
        },
    }
    plan = {"transaction_id": "bootstrap-transaction-000001"}
    cutover_journal = {
        "phases": {
            "staged": {
                "bootstrap_evidence": {
                    "prepared": prepared,
                }
            }
        }
    }
    monkeypatch.setattr(
        cutover,
        "_read_bootstrap_journal",
        lambda _plan: {
            "phases": {
                "prepared": prepared,
                "legacy_gateway_drain_intent": drain_intent,
            }
        },
    )
    monkeypatch.setattr(
        cutover,
        "_upgrade_internal_watchdog_prepared_receipt",
        lambda _plan, actual: actual,
    )

    assert cutover._bootstrap_rollback_context(
        plan,
        cutover_journal,
    ) == {
        "prepared": prepared,
        "drain_intent": drain_intent,
    }


def test_release_commit_reports_watchdog_barrier_finish_failure(monkeypatch):
    monkeypatch.setattr(
        cutover,
        "_reconcile_cutover_journal",
        lambda *_args, **_kwargs: {"phases": {}},
    )
    monkeypatch.setattr(
        cutover,
        "_ensure_gateway_last_good_attested",
        lambda _plan, journal: journal,
        raising=False,
    )
    monkeypatch.setattr(
        cutover,
        "_begin_release_watchdog_barrier",
        lambda *_args, **_kwargs: {"status": "held"},
    )
    monkeypatch.setattr(
        cutover,
        "_run_release_commit_plan_core",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cutover.ReleaseBuildError("candidate failed")
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_finish_release_watchdog_barrier",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cutover.ReleaseBuildError("barrier failed")
        ),
    )

    with pytest.raises(
        cutover.ReleaseBuildError,
        match=(
            "paired release failed: candidate failed; "
            "watchdog barrier finish failed: barrier failed"
        ),
    ):
        cutover._run_release_commit_plan({})


def test_gateway_last_good_attestation_is_idempotent_before_barrier(monkeypatch):
    plan = {
        "transaction_id": "gateway-before-barrier-transaction-0001",
        "transaction_journal": "/tmp/gateway-before-barrier.json",
        "last_good_identity": {"build_id": "last-good"},
    }
    journal = {"phases": {"staged": {}, "plist_installed": {}}}
    binding = {"status": "verified", "build_id": "last-good"}
    calls = {"attest": 0, "record": 0}

    def attest(actual_plan, identity):
        assert actual_plan is plan
        assert identity == plan["last_good_identity"]
        calls["attest"] += 1
        return binding

    def record(path, *, transaction_id, phase, receipt):
        assert path == plan["transaction_journal"]
        assert transaction_id == plan["transaction_id"]
        assert phase == "gateway_last_good_attested"
        assert receipt == {"binding": binding}
        calls["record"] += 1
        return {
            "phases": {
                **journal["phases"],
                phase: receipt,
            }
        }

    monkeypatch.setattr(cutover, "_attest_managed_gateway_binding", attest)
    monkeypatch.setattr(cutover, "record_transaction_phase", record)

    attested = cutover._ensure_gateway_last_good_attested(plan, journal)
    assert cutover._ensure_gateway_last_good_attested(plan, attested) == attested
    assert cutover._ensure_gateway_last_good_attested(
        plan,
        {"phases": {"rollback_started": {}}},
    ) == {"phases": {"rollback_started": {}}}
    assert calls == {"attest": 1, "record": 1}


def test_release_commit_reconciles_journal_before_watchdog_barrier(monkeypatch):
    plan = {"transaction_id": "journal-before-barrier-transaction-0001"}
    barrier = {"status": "held"}
    events = []

    def reconcile(actual_plan):
        assert actual_plan is plan
        events.append("reconcile-journal")
        return {"phases": {}}

    def begin(actual_plan, *, prepared=None):
        assert actual_plan is plan
        assert prepared is None
        assert events == [
            "reconcile-journal",
            "attest-last-good-gateway",
        ]
        events.append("begin-watchdog-barrier")
        return barrier

    def core(actual_plan, **_kwargs):
        assert actual_plan is plan
        events.append("run-cutover")
        return {"status": "accepted"}

    def finish(actual_plan, actual_barrier):
        assert actual_plan is plan
        assert actual_barrier is barrier
        events.append("finish-watchdog-barrier")
        return {"status": "released"}

    monkeypatch.setattr(cutover, "_reconcile_cutover_journal", reconcile)
    monkeypatch.setattr(
        cutover,
        "_ensure_gateway_last_good_attested",
        lambda actual_plan, journal: events.append(
            "attest-last-good-gateway"
        )
        or journal
        if actual_plan is plan
        else pytest.fail("wrong plan"),
        raising=False,
    )
    monkeypatch.setattr(cutover, "_begin_release_watchdog_barrier", begin)
    monkeypatch.setattr(cutover, "_run_release_commit_plan_core", core)
    monkeypatch.setattr(cutover, "_finish_release_watchdog_barrier", finish)

    result = cutover._run_release_commit_plan(plan)

    assert result == {
        "status": "accepted",
        "watchdog_barrier": {"status": "released"},
    }
    assert events == [
        "reconcile-journal",
        "attest-last-good-gateway",
        "begin-watchdog-barrier",
        "run-cutover",
        "finish-watchdog-barrier",
    ]


def test_release_commit_passes_held_watchdog_barrier_to_managed_readiness(
    monkeypatch,
):
    plan = {"transaction_id": "managed-barrier-readiness-transaction-0001"}
    barrier = {"status": "held"}
    readiness = {"status": "verified-disabled-barrier"}
    events = []

    monkeypatch.setattr(
        cutover,
        "_reconcile_cutover_journal",
        lambda actual_plan: {"phases": {}}
        if actual_plan is plan
        else pytest.fail("wrong plan"),
    )
    monkeypatch.setattr(
        cutover,
        "_ensure_gateway_last_good_attested",
        lambda actual_plan, journal: journal
        if actual_plan is plan
        else pytest.fail("wrong plan"),
    )
    monkeypatch.setattr(
        cutover,
        "_begin_release_watchdog_barrier",
        lambda actual_plan, *, prepared=None: barrier
        if actual_plan is plan and prepared is None
        else pytest.fail("wrong barrier input"),
    )
    monkeypatch.setattr(
        cutover,
        "_attest_release_watchdog_barrier",
        lambda actual_plan, actual_barrier: events.append("attest-held")
        or readiness
        if actual_plan is plan and actual_barrier is barrier
        else pytest.fail("wrong held barrier"),
        raising=False,
    )

    def core(actual_plan, *, managed_watchdog_readiness=None, **_kwargs):
        assert actual_plan is plan
        assert managed_watchdog_readiness is not None
        assert managed_watchdog_readiness() is readiness
        assert managed_watchdog_readiness() is readiness
        return {"status": "accepted"}

    monkeypatch.setattr(cutover, "_run_release_commit_plan_core", core)
    monkeypatch.setattr(
        cutover,
        "_finish_release_watchdog_barrier",
        lambda actual_plan, actual_barrier: {"status": "released"}
        if actual_plan is plan and actual_barrier is barrier
        else pytest.fail("wrong barrier"),
    )

    result = cutover._run_release_commit_plan(plan)

    assert result["status"] == "accepted"
    assert result["watchdog_barrier"] == {"status": "released"}
    assert events == ["attest-held", "attest-held"]


def test_release_commit_resumes_after_crash_between_reconcile_and_barrier(
    monkeypatch,
):
    plan = {"transaction_id": "journal-before-barrier-crash-transaction-0001"}
    barrier = {"status": "held"}
    calls = {
        "reconcile": 0,
        "attest_gateway": 0,
        "begin": 0,
        "core": 0,
        "finish": 0,
    }

    def reconcile(actual_plan):
        assert actual_plan is plan
        calls["reconcile"] += 1
        return {"phases": {"staged": {}, "plist_installed": {}}}

    def begin(actual_plan, *, prepared=None):
        assert actual_plan is plan
        assert prepared is None
        calls["begin"] += 1
        if calls["begin"] == 1:
            raise cutover.InjectedCutoverCrash("before-watchdog-barrier")
        return barrier

    def core(actual_plan, **_kwargs):
        assert actual_plan is plan
        calls["core"] += 1
        return {"status": "accepted"}

    def finish(actual_plan, actual_barrier):
        assert actual_plan is plan
        assert actual_barrier is barrier
        calls["finish"] += 1
        return {"status": "released"}

    monkeypatch.setattr(cutover, "_reconcile_cutover_journal", reconcile)
    monkeypatch.setattr(
        cutover,
        "_ensure_gateway_last_good_attested",
        lambda actual_plan, journal: calls.__setitem__(
            "attest_gateway",
            calls["attest_gateway"] + 1,
        )
        or journal
        if actual_plan is plan
        else pytest.fail("wrong plan"),
        raising=False,
    )
    monkeypatch.setattr(cutover, "_begin_release_watchdog_barrier", begin)
    monkeypatch.setattr(cutover, "_run_release_commit_plan_core", core)
    monkeypatch.setattr(cutover, "_finish_release_watchdog_barrier", finish)

    with pytest.raises(
        cutover.InjectedCutoverCrash,
        match="before-watchdog-barrier",
    ):
        cutover._run_release_commit_plan(plan)

    result = cutover._run_release_commit_plan(plan)

    assert result["status"] == "accepted"
    assert result["watchdog_barrier"] == {"status": "released"}
    assert calls == {
        "reconcile": 2,
        "attest_gateway": 2,
        "begin": 2,
        "core": 1,
        "finish": 1,
    }


def test_release_commit_prepares_bootstrap_watchdog_before_state_lock(
    monkeypatch,
):
    expected_identity = {
        "build_id": "candidate-r31",
        "selector_generation": 79,
    }
    plan = {"expected_candidate_identity": expected_identity}
    prepared = {"watchdog_cron": {"status": "disabled"}}
    readiness = {
        "status": "ready",
        "watchdog": {"script": {"sha256": "a" * 64}},
    }
    barrier = {"status": "held"}
    events = []

    def prepare(identity):
        assert identity == expected_identity
        events.append("prepare-watchdog")
        return readiness

    def begin(actual_plan, *, prepared=None):
        assert actual_plan is plan
        assert prepared is prepared_receipt
        events.append("acquire-state-lock")
        return barrier

    def core(actual_plan, *, bootstrap_prepare_pair=None, **_kwargs):
        assert actual_plan is plan
        events.append("run-cutover")
        assert bootstrap_prepare_pair is not None
        signed_process_identity = {
            **expected_identity,
            "pid": 41,
            "pid_start_token": "candidate-start",
            "instance_id": "candidate-instance",
        }
        assert bootstrap_prepare_pair(signed_process_identity) == readiness
        with pytest.raises(
            cutover.DrainIdentityMismatch,
            match="bootstrap paired readiness candidate changed",
        ):
            bootstrap_prepare_pair(
                {
                    **signed_process_identity,
                    "build_id": "different-candidate",
                }
            )
        events.append("use-cached-readiness")
        return {"status": "accepted"}

    prepared_receipt = prepared
    monkeypatch.setattr(
        cutover,
        "_reconcile_cutover_journal",
        lambda actual_plan: events.append("reconcile-journal")
        or {"phases": {}}
        if actual_plan is plan
        else pytest.fail("wrong plan"),
    )
    monkeypatch.setattr(
        cutover,
        "_ensure_gateway_last_good_attested",
        lambda actual_plan, journal: events.append(
            "attest-last-good-gateway"
        )
        or journal
        if actual_plan is plan
        else pytest.fail("wrong plan"),
        raising=False,
    )
    monkeypatch.setattr(cutover, "_begin_release_watchdog_barrier", begin)
    monkeypatch.setattr(cutover, "_run_release_commit_plan_core", core)
    monkeypatch.setattr(
        cutover,
        "_finish_release_watchdog_barrier",
        lambda actual_plan, actual_barrier: events.append("release-state-lock")
        or {"status": "released"}
        if actual_plan is plan and actual_barrier is barrier
        else pytest.fail("wrong barrier"),
    )

    result = cutover._run_release_commit_plan(
        plan,
        bootstrap_prepare_pair=prepare,
        watchdog_prepared=prepared_receipt,
    )

    assert result["watchdog_barrier"] == {"status": "released"}
    assert events == [
        "prepare-watchdog",
        "reconcile-journal",
        "attest-last-good-gateway",
        "acquire-state-lock",
        "run-cutover",
        "use-cached-readiness",
        "release-state-lock",
    ]


def test_release_core_uses_barrier_readiness_while_watchdog_is_disabled(
    monkeypatch,
):
    transaction_id = "disabled-watchdog-core-transaction-0001"
    candidate_identity = {
        "build_id": "candidate-r47",
        "selector_generation": 95,
    }
    paired_keys = (
        cutover._BOOTSTRAP_GATEWAY_PLAN_KEYS
        | cutover._BOOTSTRAP_WATCHDOG_PLAN_KEYS
        | cutover._BOOTSTRAP_LEGACY_BOUNDARY_PLAN_KEYS
    )
    plan = {key: f"unused-{key}" for key in paired_keys}
    plan.update(
        {
            "transaction_id": transaction_id,
            "transaction_journal": "transaction.json",
            "expected_candidate_identity": candidate_identity,
            "last_good_identity": {"build_id": "last-good"},
            "base_url": "http://127.0.0.1:8787",
            "signing_key_file": "release-control.key",
            "timeout_seconds": 1,
            "interval_seconds": 0.01,
        }
    )
    gateway_binding = {"status": "verified"}
    gate_intent = {"transaction_id": transaction_id}
    gate_installed = {
        "owner_hash": "8" * 64,
        "payload_sha256": "9" * 64,
    }
    drain_intent = {"transaction_id": transaction_id}
    journal = {
        "rollback_receipt": {},
        "phases": {
            "gateway_last_good_attested": {"status": "verified"},
            "candidate_gateway_accepted": {"binding": gateway_binding},
            "pair_gate_install_intent": gate_intent,
            "pair_gate_installed": gate_installed,
            "gateway_drain_intent": {"intent": drain_intent},
        },
    }
    readiness_receipts = [
        {"status": "verified-disabled-barrier", "sequence": 1},
        {"status": "verified-disabled-barrier", "sequence": 2},
        {"status": "verified-disabled-barrier", "sequence": 3},
    ]
    readiness_calls = []
    events = []
    monkeypatch.setattr(
        cutover,
        "_reconcile_cutover_journal",
        lambda _plan: copy.deepcopy(journal),
    )
    monkeypatch.setattr(
        cutover,
        "_bootstrap_rollback_context",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cutover,
        "_release_control_client",
        lambda *_args, **_kwargs: (
            lambda: {"status": "inspected"},
            lambda *_args, **_kwargs: {"status": "unused"},
            transaction_id,
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_read_release_control_key",
        lambda _path: b"test-release-control-key",
    )
    monkeypatch.setattr(
        cutover,
        "read_transaction_journal",
        lambda *_args, **_kwargs: copy.deepcopy(journal),
    )
    monkeypatch.setattr(
        cutover,
        "_complete_candidate_gateway_transition",
        lambda *_args, **_kwargs: {"gateway": gateway_binding},
    )
    monkeypatch.setattr(
        cutover,
        "_install_or_adopt_pair_open_gate",
        lambda *_args, **_kwargs: copy.deepcopy(gate_installed),
    )
    monkeypatch.setattr(
        cutover,
        "_expected_agent_pair_gate_receipt",
        lambda _intent, *, active: {"active": active},
    )
    monkeypatch.setattr(
        cutover,
        "_clear_legacy_gateway_drain_marker",
        lambda _plan, actual_intent: {"status": "cleared"}
        if actual_intent == drain_intent
        else pytest.fail("wrong drain intent"),
    )
    monkeypatch.setattr(
        cutover,
        "_attest_managed_gateway_binding",
        lambda *_args, **kwargs: {
            "health": {
                "drain": {"pair_open_gate": kwargs["expected_pair_gate"]}
            }
        },
    )
    monkeypatch.setattr(
        cutover,
        "_collect_process_binding",
        lambda *_args, **_kwargs: {"status": "verified"},
    )
    monkeypatch.setattr(
        cutover,
        "_require_candidate_binding",
        lambda binding, **_kwargs: binding,
    )
    monkeypatch.setattr(
        cutover,
        "_require_webui_pair_gate_state",
        lambda _binding, _intent, *, active: {"active": active},
    )
    monkeypatch.setattr(
        cutover,
        "_pair_open_gate_release_state",
        lambda *_args, **_kwargs: "active",
    )
    monkeypatch.setattr(
        cutover,
        "_release_owned_pair_open_gate",
        lambda *_args, **_kwargs: events.append("release-gate")
        or {"status": "released"},
    )
    monkeypatch.setattr(
        cutover,
        "_wait_for_expected_binding",
        lambda *_args, **_kwargs: {"status": "verified"},
    )
    monkeypatch.setattr(
        cutover,
        "_attest_managed_watchdog_readiness",
        lambda _plan: pytest.fail("disabled watchdog cannot attest as active"),
    )

    def managed_readiness():
        receipt = readiness_receipts[len(readiness_calls)]
        readiness_calls.append(receipt)
        events.append(f"readiness-{receipt['sequence']}")
        return receipt

    def run_cutover(**kwargs):
        prepared = kwargs["prepare_pair_before_commit"](candidate_identity)
        opened = kwargs["open_pair_after_promotion"](candidate_identity)
        kwargs["release_pair_after_acceptance"](
            candidate_identity,
            gate_installed,
        )
        assert prepared["pre_open"] == readiness_receipts[0]
        assert opened["open"]["live_readiness"] == readiness_receipts[1]
        return {"status": "accepted"}

    monkeypatch.setattr(cutover, "run_release_control_cutover", run_cutover)

    assert cutover._run_release_commit_plan_core(
        plan,
        managed_watchdog_readiness=managed_readiness,
    ) == {"status": "accepted"}
    assert readiness_calls == readiness_receipts
    assert events == [
        "readiness-1",
        "readiness-2",
        "readiness-3",
        "release-gate",
    ]


@pytest.mark.parametrize("gate_release_state", ("active", "released"))
def test_release_commit_uses_sealed_identity_for_gateway_pair_attestation(
    monkeypatch,
    gate_release_state,
):
    transaction_id = "sealed-gateway-attestation-transaction-0001"
    sealed_identity = {
        "build_id": "candidate-r35",
        "commit": "1" * 40,
        "tree": "2" * 40,
        "manifest_sha256": "3" * 64,
        "agent_source_commit": "4" * 40,
        "agent_source_tree": "5" * 40,
        "agent_source_manifest_sha256": "6" * 64,
        "runtime_manifest_sha256": "7" * 64,
        "selector_generation": 84,
    }
    signed_process_identity = {
        "build_id": sealed_identity["build_id"],
        "commit": sealed_identity["commit"],
        "tree": sealed_identity["tree"],
        "manifest_sha256": sealed_identity["manifest_sha256"],
        "agent_commit": sealed_identity["agent_source_commit"],
        "agent_tree": sealed_identity["agent_source_tree"],
        "agent_manifest_sha256": sealed_identity[
            "agent_source_manifest_sha256"
        ],
        "runtime_manifest_sha256": sealed_identity[
            "runtime_manifest_sha256"
        ],
        "selector_generation": sealed_identity["selector_generation"],
        "pid": 71,
        "pid_start_token": "candidate-start",
    }
    paired_keys = (
        cutover._BOOTSTRAP_GATEWAY_PLAN_KEYS
        | cutover._BOOTSTRAP_WATCHDOG_PLAN_KEYS
        | cutover._BOOTSTRAP_LEGACY_BOUNDARY_PLAN_KEYS
    )
    plan = {key: f"unused-{key}" for key in paired_keys}
    plan.update(
        {
            "transaction_id": transaction_id,
            "transaction_journal": "transaction.json",
            "expected_candidate_identity": sealed_identity,
            "last_good_identity": {"build_id": "last-good"},
            "base_url": "http://127.0.0.1:8787",
            "signing_key_file": "release-control.key",
            "timeout_seconds": 1,
            "interval_seconds": 0.01,
        }
    )
    gate_intent = {"transaction_id": transaction_id}
    gate_installed = {
        "owner_hash": "8" * 64,
        "payload_sha256": "9" * 64,
    }
    journal = {
        "rollback_receipt": {},
        "phases": {
            "gateway_last_good_attested": {"status": "verified"},
            "pair_gate_install_intent": gate_intent,
            "pair_gate_installed": gate_installed,
        },
    }
    gateway_identities = []
    candidate_health_proofs = []
    process_exit_waits = []
    old_process_identity = {
        "pid": 41,
        "pid_start_token": "old-process-start",
    }
    monkeypatch.setattr(
        cutover,
        "_reconcile_cutover_journal",
        lambda _plan: copy.deepcopy(journal),
    )
    monkeypatch.setattr(
        cutover,
        "_bootstrap_rollback_context",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cutover,
        "_release_control_client",
        lambda *_args, **_kwargs: (
            lambda: {"status": "inspected"},
            lambda *_args, **_kwargs: {"status": "unused"},
            transaction_id,
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_read_release_control_key",
        lambda _path: b"test-release-control-key",
    )
    monkeypatch.setattr(
        cutover,
        "read_transaction_journal",
        lambda *_args, **_kwargs: copy.deepcopy(journal),
    )
    monkeypatch.setattr(
        cutover,
        "_install_or_adopt_pair_open_gate",
        lambda *_args, **_kwargs: copy.deepcopy(gate_installed),
    )
    monkeypatch.setattr(
        cutover,
        "_expected_agent_pair_gate_receipt",
        lambda _intent, *, active: {"active": active},
    )

    def attest_gateway(_plan, identity, **_kwargs):
        gateway_identities.append(identity)
        return {
            "health": {
                "drain": {
                    "pair_open_gate": {
                        "active": _kwargs["expected_admission"]
                        == "rejecting_new_work"
                    }
                }
            }
        }

    monkeypatch.setattr(
        cutover,
        "_attest_managed_gateway_binding",
        attest_gateway,
    )
    monkeypatch.setattr(
        cutover,
        "_collect_process_binding",
        lambda *_args, **_kwargs: {"status": "verified"},
    )
    def require_candidate(binding, **kwargs):
        candidate_health_proofs.append((binding, kwargs))
        return binding

    monkeypatch.setattr(cutover, "_require_candidate_binding", require_candidate)
    monkeypatch.setattr(
        cutover,
        "_require_webui_pair_gate_state",
        lambda _binding, _intent, *, active: {"active": active},
    )
    monkeypatch.setattr(
        cutover,
        "_pair_open_gate_release_state",
        lambda *_args, **_kwargs: gate_release_state,
    )
    monkeypatch.setattr(
        cutover,
        "_release_owned_pair_open_gate",
        lambda *_args, **_kwargs: {
            "status": (
                "released"
                if gate_release_state == "active"
                else "already-released"
            )
        },
    )
    monkeypatch.setattr(
        cutover,
        "_wait_for_expected_binding",
        lambda *_args, **_kwargs: {"status": "verified"},
    )
    monkeypatch.setattr(
        cutover,
        "wait_for_exact_process_exit",
        lambda identity,
        timeout,
        *,
        allow_exact_signaled_zombie=False: process_exit_waits.append(
            (identity, timeout, allow_exact_signaled_zombie)
        ),
    )

    def run_cutover(**kwargs):
        kwargs["wait_for_process_exit"](old_process_identity, 1.0)
        kwargs["open_pair_after_promotion"](signed_process_identity)
        kwargs["release_pair_after_acceptance"](
            signed_process_identity,
            gate_installed,
        )
        return {"status": "accepted"}

    monkeypatch.setattr(cutover, "run_release_control_cutover", run_cutover)

    assert cutover._run_release_commit_plan_core(
        plan,
        bootstrap_open_pair=lambda _identity: {"status": "ready"},
    ) == {"status": "accepted"}
    expected_proof_count = 2 if gate_release_state == "active" else 1
    assert gateway_identities == [sealed_identity] * (
        3 if gate_release_state == "active" else 2
    )
    assert len(candidate_health_proofs) == expected_proof_count
    assert process_exit_waits == [(old_process_identity, 1.0, True)]
    assert all(
        proof[1]["admission_state"] == "open"
        and proof[1]["require_full_health"] is True
        for proof in candidate_health_proofs
    )


def test_gateway_binding_wait_does_not_retry_programming_errors(monkeypatch):
    calls = {"count": 0}

    def broken_job_probe(_plan, *, gateway):
        assert gateway is True
        calls["count"] += 1
        raise KeyError("agent_source_commit")

    monkeypatch.setattr(cutover, "_job_pid", broken_job_probe)

    with pytest.raises(KeyError, match="agent_source_commit"):
        cutover._wait_for_gateway_binding(
            {
                "timeout_seconds": 0.01,
                "interval_seconds": 0,
                "gateway_listener_port": 8642,
            },
            previous_pid_start=None,
        )
    assert calls == {"count": 1}


def test_managed_gateway_attestation_uses_last_good_originating_transaction(
    tmp_path,
    monkeypatch,
):
    originating_transaction = "managed-gateway-origin-transaction-0001"
    new_transaction = "managed-gateway-next-transaction-0000001"
    identity = {
        "build_id": "candidate-r32",
        "commit": "1" * 40,
        "tree": "2" * 40,
        "manifest_sha256": "3" * 64,
        "interpreter_path": "/sealed/python",
        "agent_source_path": "/sealed/agent",
        "agent_source_commit": "4" * 40,
        "agent_source_tree": "5" * 40,
        "agent_source_manifest_sha256": "6" * 64,
        "runtime_path": "/sealed/runtime",
        "runtime_python_home_path": "/sealed/runtime/python-home",
        "runtime_site_packages_path": "/sealed/runtime/site-packages",
        "runtime_manifest_sha256": "7" * 64,
        "release_path": "/sealed/webui",
        "selector_generation": 82,
        "startup_transaction_id": originating_transaction,
    }
    restarted_webui_identity = {
        **identity,
        "selector_generation": 90,
        "startup_fenced": False,
        "startup_transaction_id": None,
    }
    routing = {
        "HERMES_WEBUI_DEFAULT_MODEL": "model",
        "HERMES_WEBUI_DEFAULT_PROVIDER": "provider",
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_PORT": "8787",
    }
    pair_id = "pair-originating-transaction"
    arguments = ["/sealed/hermes", "gateway", "run", "--replace"]
    environment = {
        **routing,
        "PYTHONHOME": identity["runtime_python_home_path"],
        "PYTHONPATH": os.pathsep.join(
            [
                identity["agent_source_path"],
                identity["runtime_site_packages_path"],
            ]
        ),
        "HERMES_WEBUI_RELEASE_PATH": identity["release_path"],
        "HERMES_WEBUI_MANIFEST_SHA256": identity["manifest_sha256"],
        "HERMES_WEBUI_AGENT_DIR": identity["agent_source_path"],
        "HERMES_WEBUI_AGENT_MANIFEST_SHA256": identity[
            "agent_source_manifest_sha256"
        ],
        "HERMES_WEBUI_RUNTIME_PATH": identity["runtime_path"],
        "HERMES_WEBUI_RUNTIME_MANIFEST_SHA256": identity[
            "runtime_manifest_sha256"
        ],
        "HERMES_WEBUI_LAUNCH_MODE": "managed-gateway",
        "HERMES_AGENT_COMMIT": identity["agent_source_commit"],
        "HERMES_AGENT_TREE": identity["agent_source_tree"],
        "HERMES_AGENT_MANIFEST_SHA256": identity[
            "agent_source_manifest_sha256"
        ],
        "HERMES_AGENT_SOURCE_PATH": identity["agent_source_path"],
        "HERMES_RUNTIME_MANIFEST_SHA256": identity[
            "runtime_manifest_sha256"
        ],
        "HERMES_RUNTIME_PATH": identity["runtime_path"],
        "HERMES_RELEASE_PAIR_ID": pair_id,
        "HERMES_WEBUI_BUILD_ID": identity["build_id"],
        "HERMES_WEBUI_COMMIT": identity["commit"],
        "HERMES_WEBUI_TREE": identity["tree"],
        "HERMES_SELECTOR_GENERATION": str(identity["selector_generation"]),
        "HERMES_RELEASE_TRANSACTION_ID": originating_transaction,
        "HERMES_GATEWAY_LAUNCHD_LABEL": "ai.hermes.gateway",
    }
    installed = {
        "Label": "ai.hermes.gateway",
        "ProgramArguments": arguments,
        "EnvironmentVariables": environment,
        "WorkingDirectory": identity["agent_source_path"],
    }
    installed_path = tmp_path / "installed.plist"
    rollback_path = tmp_path / "rollback.plist"
    installed_path.write_text("installed")
    rollback_path.write_text("rollback")
    plan = {
        "transaction_id": new_transaction,
        "gateway_installed_plist": str(installed_path),
        "gateway_rollback_plist": str(rollback_path),
        "gateway_launchd_label": "ai.hermes.gateway",
        "last_good_identity": restarted_webui_identity,
        "last_good_gateway_identity": identity,
    }
    shim_sha256 = hashlib.sha256(b"sealed-shim").hexdigest()
    program = {"sha256": shim_sha256}
    binding = {
        "status": "verified",
        "runtime": {
            "program_identity": program,
            "program_arguments": arguments,
            "cwd": identity["agent_source_path"],
        },
    }
    observed_transactions = []

    monkeypatch.setattr(
        cutover,
        "_wait_for_gateway_binding",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(
        cutover,
        "_read_plist",
        lambda path: (
            installed
            if Path(path) == installed_path
            else {"ProgramArguments": arguments}
        ),
    )
    monkeypatch.setattr(cutover, "_render_cli_shim", lambda _identity: b"sealed-shim")
    monkeypatch.setattr(
        cutover,
        "_file_identity_receipt",
        lambda path: program if str(path) == arguments[0] else {"path": str(path)},
    )
    monkeypatch.setattr(cutover, "_managed_gateway_routing", lambda _plan: routing)

    def transform(*_args, release_transaction_id, **_kwargs):
        observed_transactions.append(("transform", release_transaction_id))
        return installed

    def release_pair_id(
        _identity,
        *,
        selector_generation,
        transaction_id,
    ):
        assert selector_generation == 82
        observed_transactions.append(("pair", transaction_id))
        return pair_id

    monkeypatch.setattr(cutover, "transform_gateway_launchd_target", transform)
    monkeypatch.setattr(
        cutover.release_selector,
        "release_pair_id",
        release_pair_id,
    )

    result = cutover._attest_managed_gateway_binding(
        plan,
        restarted_webui_identity,
    )

    assert result["build_id"] == identity["build_id"]
    assert observed_transactions == [
        ("transform", originating_transaction),
        ("pair", originating_transaction),
    ]


def test_rollback_gateway_stop_intent_drains_restored_legacy_without_managed_wait(
    tmp_path,
    monkeypatch,
):
    installed = tmp_path / "installed-gateway.plist"
    rollback = tmp_path / "rollback-gateway.plist"
    managed = tmp_path / "managed-gateway.plist"
    installed.write_bytes(b"legacy gateway plist\n")
    rollback.write_bytes(b"legacy gateway plist\n")
    managed.write_bytes(b"managed gateway plist\n")
    plan = {
        "gateway_listener_port": 8642,
        "gateway_installed_plist": str(installed),
        "gateway_rollback_plist": str(rollback),
        "managed_gateway_plist": str(managed),
        "expected_candidate_identity": {"build_id": "candidate-r29"},
    }
    captured = {
        "gateway": {
            "pid": 31,
            "pid_start_token": "retired-gateway",
        }
    }
    drain_intent = {
        "status": "prepared",
        "marker": {"sha256": "d" * 64},
    }
    restored_binding = {
        "status": "verified",
        "pid": 51,
        "pid_start_token": "restored-gateway",
    }
    events = []
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 51)
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: 51,
    )
    monkeypatch.setattr(
        cutover,
        "_attest_restored_legacy_binding",
        lambda _plan, *, prepared, gateway: (
            events.append(("legacy-attest", prepared, gateway))
            or restored_binding
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_attest_managed_gateway_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact restored legacy must not enter managed wait")
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_wait_for_legacy_gateway_drain",
        lambda _plan, prepared, intent: (
            events.append(("legacy-drain", prepared, intent))
            or {"status": "drained"}
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_legacy_gateway_stop_intent_receipt",
        lambda _plan, prepared, drained: {
            "status": "prepared",
            "prepared": prepared,
            "drained": drained,
        },
    )

    receipt = cutover._rollback_gateway_stop_intent(
        plan,
        prepared=captured,
        legacy_drain_intent=drain_intent,
    )

    live_prepared = {
        "gateway": {
            "pid": 51,
            "pid_start_token": "restored-gateway",
        }
    }
    assert receipt == {
        "status": "prepared",
        "runtime_mode": "restored-legacy",
        "prepared": live_prepared,
        "drain_intent": drain_intent,
        "drain_receipt": {"status": "drained"},
        "intent": {
            "status": "prepared",
            "prepared": live_prepared,
            "drained": {"status": "drained"},
        },
    }
    assert events == [
        ("legacy-attest", captured, True),
        ("legacy-drain", live_prepared, drain_intent),
    ]


def test_bootstrap_rollback_authorizes_exact_restarted_legacy_webui(
    monkeypatch,
):
    plan = {
        "gateway_listener_port": 8642,
        "listener_port": 8787,
    }
    journal = {"phases": {"prepared": {"captured": "legacy"}}}
    restored_runtime = {
        "pid": 77,
        "pid_start_token": "restarted-legacy",
        "command": "legacy command",
    }
    captured_authorizations = []
    monkeypatch.setattr(
        cutover,
        "_acquire_legacy_cron_tick_lock",
        lambda _plan: {"status": "held"},
    )
    monkeypatch.setattr(
        cutover,
        "_verify_legacy_cron_tick_lock",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: None)
    monkeypatch.setattr(cutover, "_job_pid", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cutover,
        "_listener_pid_or_none",
        lambda port: 77 if port == 8787 else None,
    )
    monkeypatch.setattr(
        cutover,
        "_authorized_bootstrap_runtimes",
        lambda *_args, **_kwargs: [{"pid": 31}],
    )
    monkeypatch.setattr(
        cutover,
        "_authorized_cutover_runtimes",
        lambda *_args, **_kwargs: [{"pid": 41}],
    )
    monkeypatch.setattr(
        cutover,
        "_restored_legacy_runtime_authorization",
        lambda _plan, *, prepared, gateway: (
            restored_runtime
            if prepared == journal["phases"]["prepared"] and gateway is False
            else None
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_incomplete_managed_webui_stop_authorization",
        lambda *_args, **_kwargs: None,
    )

    def stop_current(_plan, *, gateway, authorized_receipts):
        assert gateway is False
        captured_authorizations.extend(authorized_receipts)
        return {"status": "stopped"}

    monkeypatch.setattr(cutover, "_stop_current_service", stop_current)

    receipt = cutover._stop_bootstrap_pair_for_rollback(
        plan,
        journal,
        {"status": "not-running"},
    )

    assert receipt["webui"] == {"status": "stopped"}
    assert restored_runtime in captured_authorizations


def test_restart_or_adopt_restored_legacy_pair_adopts_exact_running_pair(
    monkeypatch,
):
    bindings = {
        True: {
            "status": "verified",
            "pid": 41,
            "pid_start_token": "gateway-start",
        },
        False: {
            "status": "verified",
            "pid": 42,
            "pid_start_token": "webui-start",
        },
    }
    attestations = []

    def attest(_plan, *, prepared, gateway):
        assert prepared == {"captured": "legacy-pair"}
        attestations.append(gateway)
        return bindings[gateway]

    monkeypatch.setattr(cutover, "_attest_restored_legacy_binding", attest)
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact restored pair must not be booted out")
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_bootstrap_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact restored pair must not be restarted")
        ),
    )

    receipt = cutover._restart_or_adopt_restored_legacy_pair(
        {},
        prepared={"captured": "legacy-pair"},
    )

    assert attestations == [True, False]
    assert receipt["gateway_binding"] == bindings[True]
    assert receipt["webui_binding"] == bindings[False]
    assert receipt["gateway_start"] == {
        "status": "adopted-exact-restored-binding",
        "pid": 41,
        "pid_start_token": "gateway-start",
    }
    assert receipt["webui_start"] == {
        "status": "adopted-exact-restored-binding",
        "pid": 42,
        "pid_start_token": "webui-start",
    }


def test_restored_legacy_gateway_allows_runtime_cwd_drift_only(monkeypatch):
    expected = {
        "command": "/legacy/python -m hermes_cli.main gateway run --replace",
        "comm": "/legacy/python",
        "cwd": "/runtime/selected-workspace",
        "program_arguments": [
            "/legacy/python",
            "-m",
            "hermes_cli.main",
            "gateway",
            "run",
            "--replace",
        ],
        "program_identity": {
            "path": "/legacy/python",
            "sha256": "a" * 64,
        },
    }
    actual = {
        **expected,
        "cwd": "/launchd/working-directory",
        "pid": 51,
        "pid_start_token": "restored-gateway-start",
    }
    monkeypatch.setattr(
        cutover,
        "_listener_process_receipt",
        lambda *_args, **_kwargs: actual,
    )
    monkeypatch.setattr(
        cutover,
        "_gateway_health_receipt",
        lambda _plan: {"status": "ok"},
    )

    receipt = cutover._attest_restored_legacy_binding(
        {},
        prepared={"gateway": expected},
        gateway=True,
    )

    assert receipt["status"] == "verified"
    assert receipt["pid"] == 51

    actual["command"] = "/unexpected/python -m hermes_cli.main gateway run"
    with pytest.raises(
        cutover.ReleaseBuildError,
        match="restored legacy runtime identity changed",
    ):
        cutover._attest_restored_legacy_binding(
            {},
            prepared={"gateway": expected},
            gateway=True,
        )


def test_restart_or_adopt_restored_legacy_pair_does_not_restart_on_bug(
    monkeypatch,
):
    monkeypatch.setattr(
        cutover,
        "_attest_restored_legacy_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("injected programming error")
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected bug must not trigger a restart")
        ),
    )

    with pytest.raises(AssertionError, match="injected programming error"):
        cutover._restart_or_adopt_restored_legacy_pair(
            {},
            prepared={"captured": "legacy-pair"},
        )
