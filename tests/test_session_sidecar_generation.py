import json
import ast
import threading
from pathlib import Path

import pytest


@pytest.fixture
def isolated_session_store(tmp_path, monkeypatch):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "_write_session_index", lambda *args, **kwargs: None)
    models.SESSIONS.clear()
    yield models, session_dir
    models.SESSIONS.clear()


def _session(models, session_dir, sid="generation-test", messages=None):
    return models.Session(
        session_id=sid,
        workspace=str(session_dir),
        messages=(
            [{"role": "user", "content": "hello", "timestamp": 1}]
            if messages is None
            else messages
        ),
    )


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_legacy_missing_generation_loads_as_zero(isolated_session_store):
    models, session_dir = isolated_session_store
    sid = "legacy-generation"
    path = session_dir / f"{sid}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": sid,
                "title": "Legacy",
                "workspace": str(session_dir),
                "created_at": 1,
                "updated_at": 2,
                "messages": [{"role": "user", "content": "legacy"}],
            }
        ),
        encoding="utf-8",
    )

    assert models.Session.load(sid).sidecar_generation == 0
    assert models.Session.load_metadata_only(sid).sidecar_generation == 0


@pytest.mark.parametrize("invalid_generation", [True, 1.5, "4", -1])
def test_invalid_generation_values_fail_closed_to_zero(
    isolated_session_store,
    invalid_generation,
):
    models, session_dir = isolated_session_store
    sid = "invalid-generation"
    path = session_dir / f"{sid}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": sid,
                "title": "Invalid",
                "workspace": str(session_dir),
                "created_at": 1,
                "updated_at": 2,
                "sidecar_generation": invalid_generation,
                "messages": [],
            }
        ),
        encoding="utf-8",
    )

    session = models.Session.load(sid)
    assert session.sidecar_generation == 0
    session.save(touch_updated_at=False, skip_index=True)
    assert session.sidecar_generation == 1


def test_every_save_advances_generation_without_changing_recency_contract(
    isolated_session_store,
):
    models, session_dir = isolated_session_store
    session = _session(models, session_dir)

    session.save(skip_index=True)
    first_updated_at = session.updated_at
    assert session.sidecar_generation == 1
    assert _read(session.path)["sidecar_generation"] == 1

    session.save(touch_updated_at=False, skip_index=True)
    assert session.updated_at == first_updated_at
    assert session.sidecar_generation == 2
    assert _read(session.path)["sidecar_generation"] == 2


def test_two_stale_objects_save_distinct_monotonic_generations(
    isolated_session_store,
):
    models, session_dir = isolated_session_store
    original = _session(models, session_dir, sid="concurrent-generation")
    original.save(skip_index=True)
    first = models.Session.load(original.session_id)
    second = models.Session.load(original.session_id)
    assert first.sidecar_generation == second.sidecar_generation == 1
    barrier = threading.Barrier(3)
    errors = []

    def save_stale(session):
        try:
            barrier.wait(timeout=5)
            session.save(touch_updated_at=False, skip_index=True)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=save_stale, args=(first,)),
        threading.Thread(target=save_stale, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    assert {first.sidecar_generation, second.sidecar_generation} == {2, 3}
    assert _read(original.path)["sidecar_generation"] == 3


def test_legacy_scene_first_payload_keeps_generation_in_bounded_prefix(
    isolated_session_store,
):
    models, session_dir = isolated_session_store
    path = session_dir / "legacy-scene-first.json"
    stale_first = {
        "session_id": "legacy-scene-first",
        "anchor_activity_scenes": [{"large": "x" * 70_000}],
        "messages": [],
    }
    stale_second = dict(stale_first)

    assert models._write_session_sidecar_payload(path, stale_first) == 1
    assert models._persisted_sidecar_generation(path) == 1
    assert models._write_session_sidecar_payload(path, stale_second) == 2
    assert models._persisted_sidecar_generation(path) == 2


def test_save_over_corrupt_sidecar_starts_from_generation_one(
    isolated_session_store,
):
    models, session_dir = isolated_session_store
    session = _session(models, session_dir, sid="corrupt-generation")
    session.path.write_text(
        '{"session_id":"corrupt-generation","messages":',
        encoding="utf-8",
    )

    session.save(touch_updated_at=False, skip_index=True)

    assert session.sidecar_generation == 1
    assert _read(session.path)["sidecar_generation"] == 1


def test_failed_sidecar_replace_changes_neither_disk_nor_object_generation(
    isolated_session_store,
    monkeypatch,
):
    models, session_dir = isolated_session_store
    session = _session(models, session_dir, sid="replace-failure")
    session.save(skip_index=True)
    before = session.path.read_bytes()
    assert session.sidecar_generation == 1

    monkeypatch.setattr(
        models,
        "_safe_replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError, match="replace failed"):
        session.save(touch_updated_at=False, skip_index=True)

    assert session.sidecar_generation == 1
    assert session.path.read_bytes() == before


def test_index_failure_happens_after_generation_is_published(
    isolated_session_store,
    monkeypatch,
):
    models, session_dir = isolated_session_store
    session = _session(models, session_dir, sid="index-failure")
    session.save(skip_index=True)
    monkeypatch.setattr(
        models,
        "_write_session_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index failed")),
    )

    with pytest.raises(OSError, match="index failed"):
        session.save(touch_updated_at=False)

    assert session.sidecar_generation == 2
    assert _read(session.path)["sidecar_generation"] == 2


def test_recovery_restores_backup_with_generation_newer_than_live_and_backup(
    isolated_session_store,
):
    from api.session_recovery import recover_session

    _models, session_dir = isolated_session_store
    path = session_dir / "recover-generation.json"
    backup = path.with_suffix(".json.bak")
    path.write_text(
        json.dumps(
            {
                "session_id": "recover-generation",
                "sidecar_generation": 5,
                "messages": [{"role": "user", "content": "live"}],
            }
        ),
        encoding="utf-8",
    )
    backup.write_text(
        json.dumps(
            {
                "session_id": "recover-generation",
                "sidecar_generation": 2,
                "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = recover_session(path)

    assert result["restored"] is True
    restored = _read(path)
    assert restored["sidecar_generation"] == 6
    assert len(restored["messages"]) == 2


def test_recovery_over_corrupt_live_sidecar_advances_backup_generation(
    isolated_session_store,
):
    from api.session_recovery import recover_session

    _models, session_dir = isolated_session_store
    path = session_dir / "recover-corrupt-generation.json"
    path.write_text(
        '{"session_id":"recover-corrupt-generation","messages":',
        encoding="utf-8",
    )
    path.with_suffix(".json.bak").write_text(
        json.dumps(
            {
                "session_id": "recover-corrupt-generation",
                "sidecar_generation": 4,
                "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = recover_session(path)

    assert result["restored"] is True
    assert _read(path)["sidecar_generation"] == 5


def test_recovery_racing_live_save_never_overwrites_newer_messages(
    isolated_session_store,
):
    from api.session_recovery import recover_session

    models, session_dir = isolated_session_store
    session = _session(
        models,
        session_dir,
        sid="recovery-save-race",
        messages=[{"role": "user", "content": "live-one"}],
    )
    session.save(skip_index=True)
    session.path.with_suffix(".json.bak").write_text(
        json.dumps(
            {
                "session_id": session.session_id,
                "sidecar_generation": 1,
                "messages": [
                    {"role": "user", "content": "backup-one"},
                    {"role": "assistant", "content": "backup-two"},
                ],
            }
        ),
        encoding="utf-8",
    )
    session.messages.extend(
        [
            {"role": "assistant", "content": "new-two"},
            {"role": "user", "content": "new-three"},
        ]
    )
    results = []
    errors = []
    lock = models._session_sidecar_write_lock(session.session_id)

    def save_live():
        try:
            session.save(touch_updated_at=False, skip_index=True)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def recover_backup():
        try:
            results.append(recover_session(session.path))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with lock:
        threads = [threading.Thread(target=save_live), threading.Thread(target=recover_backup)]
        for thread in threads:
            thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert results and not any(thread.is_alive() for thread in threads)
    final = _read(session.path)
    assert [message["content"] for message in final["messages"]] == [
        "live-one",
        "new-two",
        "new-three",
    ]
    assert final["sidecar_generation"] >= 2


def test_create_only_sidecar_write_starts_at_one_and_never_overwrites(
    isolated_session_store,
):
    models, session_dir = isolated_session_store
    path = session_dir / "create-only.json"
    first = {"session_id": "create-only", "messages": []}
    second = {
        "session_id": "create-only",
        "messages": [{"role": "user", "content": "must not replace"}],
    }

    assert models._write_session_sidecar_payload(path, first, create_only=True) == 1
    assert models._write_session_sidecar_payload(path, second, create_only=True) is None
    assert _read(path)["sidecar_generation"] == 1
    assert _read(path)["messages"] == []


def test_delete_racing_recovery_cannot_resurrect_sidecar(
    isolated_session_store,
    monkeypatch,
):
    import api.session_recovery as recovery

    models, session_dir = isolated_session_store
    session = _session(models, session_dir, sid="delete-recovery-race")
    session.save(skip_index=True)
    session.path.with_suffix(".json.bak").write_text(
        json.dumps(
            {
                "session_id": session.session_id,
                "sidecar_generation": 1,
                "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                ],
            }
        ),
        encoding="utf-8",
    )
    recovery_inside_lock = threading.Event()
    allow_recovery = threading.Event()
    original_inspect = recovery.inspect_session_recovery_status

    def inspect_then_pause(path):
        status = original_inspect(path)
        recovery_inside_lock.set()
        assert allow_recovery.wait(timeout=5)
        return status

    monkeypatch.setattr(recovery, "inspect_session_recovery_status", inspect_then_pause)
    errors = []

    def restore():
        try:
            recovery.recover_session(session.path)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def delete():
        try:
            models._delete_session_sidecar_files(session.path)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    restore_thread = threading.Thread(target=restore)
    restore_thread.start()
    assert recovery_inside_lock.wait(timeout=5)
    delete_thread = threading.Thread(target=delete)
    delete_thread.start()
    allow_recovery.set()
    restore_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert errors == []
    assert not restore_thread.is_alive()
    assert not delete_thread.is_alive()
    assert not session.path.exists()
    assert not session.path.with_suffix(".json.bak").exists()


def test_delete_tombstone_beats_stale_state_db_materialization_scan(
    isolated_session_store,
    monkeypatch,
):
    import api.session_recovery as recovery

    models, session_dir = isolated_session_store
    sid = "delete-state-db-race"
    target = session_dir / f"{sid}.json"
    row = {
        "id": sid,
        "source": "webui",
        "title": "Deleted",
        "messages": [{"role": "user", "content": "must stay deleted"}],
    }
    scan_finished = threading.Event()
    allow_materialization = threading.Event()
    original_convert = recovery._state_db_row_to_sidecar

    monkeypatch.setattr(
        recovery,
        "_read_state_db_missing_sidecar_rows",
        lambda *_args, **_kwargs: [row],
    )

    def convert_after_pause(value):
        scan_finished.set()
        assert allow_materialization.wait(timeout=5)
        return original_convert(value)

    monkeypatch.setattr(recovery, "_state_db_row_to_sidecar", convert_after_pause)
    result = []
    errors = []

    def reconcile():
        try:
            result.append(
                recovery.recover_missing_sidecars_from_state_db(
                    session_dir,
                    session_dir / "state.db",
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=reconcile)
    thread.start()
    assert scan_finished.wait(timeout=5)
    with models._session_sidecar_write_lock(sid):
        models._delete_session_sidecar_files(target)
        models._record_webui_deleted_session_tombstone(sid)
    allow_materialization.set()
    thread.join(timeout=5)

    assert errors == []
    assert not thread.is_alive()
    assert not target.exists()
    assert result[0]["materialized"] == 0
    assert result[0]["details"] == [
        {
            "session_id": sid,
            "materialized": False,
            "skipped": "deleted_session_tombstone",
        }
    ]


def test_production_source_has_no_unowned_session_sidecar_mutators():
    """Discover direct sidecar writes/deletes instead of trusting a writer list."""
    root = Path(__file__).resolve().parents[1]
    violations = []

    def names(node):
        return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}

    def strings(node):
        return {
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }

    def is_sidecar_expr(node, tainted):
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return node.id in tainted or node.id in {
                "session_path",
                "sidecar_path",
                "live_path",
            }
        if isinstance(node, ast.Attribute):
            return node.attr == "path" and isinstance(node.value, ast.Name) and node.value.id in {
                "session",
                "s",
                "bg",
                "ephemeral",
            }
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "Path" and node.args:
                return is_sidecar_expr(node.args[0], tainted)
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "resolve",
                "with_suffix",
            }:
                return is_sidecar_expr(node.func.value, tainted)
        if isinstance(node, ast.BinOp):
            root_names = names(node)
            json_fragments = strings(node)
            uses_session_root = bool(
                root_names & {"SESSION_DIR", "session_dir"}
            ) or "self" in root_names
            dynamic_session_json = any(
                value.endswith(".json") and not value.endswith("_index.json")
                for value in json_fragments
            )
            return uses_session_root and dynamic_session_json
        return False

    def assigned_names(node):
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, (ast.Tuple, ast.List)):
            result = set()
            for child in node.elts:
                result.update(assigned_names(child))
            return result
        return set()

    for source_path in sorted((root / "api").glob("*.py")):
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_models_primitive = source_path.name == "models.py" and node.name in {
                "_write_session_sidecar_payload",
                "_prepare_session_shrink_backup",
                "_delete_session_sidecar_files",
                "_delete_session_sidecar_backup",
            }
            tainted = set()
            changed = True
            while changed:
                changed = False
                for child in ast.walk(node):
                    if isinstance(child, (ast.Assign, ast.AnnAssign)):
                        value = child.value
                        targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                        if is_sidecar_expr(value, tainted):
                            for target in targets:
                                before = len(tainted)
                                tainted.update(assigned_names(target))
                                changed = changed or len(tainted) != before
                    elif isinstance(child, (ast.For, ast.AsyncFor)):
                        iterator = child.iter
                        if (
                            isinstance(iterator, ast.Call)
                            and isinstance(iterator.func, ast.Attribute)
                            and iterator.func.attr == "glob"
                            and names(iterator.func.value) & {"SESSION_DIR", "session_dir"}
                            and any(value == "*.json" for value in strings(iterator))
                        ):
                            before = len(tainted)
                            tainted.update(assigned_names(child.target))
                            changed = changed or len(tainted) != before

            for call in (child for child in ast.walk(node) if isinstance(child, ast.Call)):
                direct_mutation = False
                if isinstance(call.func, ast.Attribute):
                    method = call.func.attr
                    if method in {"write_text", "unlink"}:
                        direct_mutation = is_sidecar_expr(call.func.value, tainted)
                    elif method == "replace":
                        direct_mutation = is_sidecar_expr(
                            call.func.value,
                            tainted,
                        ) or any(is_sidecar_expr(arg, tainted) for arg in call.args)
                    elif method == "link" and call.args:
                        direct_mutation = is_sidecar_expr(call.args[-1], tainted)
                elif isinstance(call.func, ast.Name):
                    if call.func.id in {"_safe_replace", "replace"} and call.args:
                        direct_mutation = is_sidecar_expr(call.args[-1], tainted)
                    elif call.func.id == "open" and call.args:
                        mode_values = (
                            strings(call.args[1]) if len(call.args) > 1 else set()
                        )
                        for keyword in call.keywords:
                            if keyword.arg == "mode":
                                mode_values.update(strings(keyword.value))
                        direct_mutation = is_sidecar_expr(
                            call.args[0],
                            tainted,
                        ) and any(set(mode) & set("wax+") for mode in mode_values)
                if direct_mutation and not is_models_primitive:
                    violations.append(
                        f"{source_path.name}:{call.lineno}:{node.name}"
                    )

    assert sorted(set(violations)) == []


def test_dormant_session_db_metadata_writes_advance_generation(
    isolated_session_store,
):
    from api.webui_session_db import WebUIJsonSessionDB

    _models, session_dir = isolated_session_store
    database = WebUIJsonSessionDB(session_dir)
    written = database.write_session(
        {
            "session_id": "adapter-generation",
            "title": "Original",
            "messages": [{"role": "user", "content": "hello"}],
            "tool_calls": [],
        }
    )
    assert written["sidecar_generation"] == 1

    database.update_metadata("adapter-generation", {"title": "Updated"})

    reloaded = database.read_session("adapter-generation")
    assert reloaded["sidecar_generation"] == 2
    assert reloaded["messages"] == written["messages"]
