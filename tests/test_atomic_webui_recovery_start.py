from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest


def _transcript_hash(messages: list[dict], last_user_id: int) -> str:
    relevant = [
        {
            "id": message.get("id"),
            "role": message.get("role"),
            "content": message.get("content"),
            "tool_call_id": message.get("tool_call_id"),
            "tool_calls": message.get("tool_calls"),
            "tool_name": message.get("tool_name"),
            "finish_reason": message.get("finish_reason"),
        }
        for message in messages
        if int(message.get("id") or 0) >= last_user_id
    ]
    raw = json.dumps(relevant, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class _Handler:
    def __init__(self, *, address="127.0.0.1", headers=None):
        self.client_address = (address, 12345)
        self.headers = headers or {}


@pytest.fixture
def recovery_state(monkeypatch, tmp_path):
    from api import config, models, routes
    from api import atomic_recovery

    # Success-path tests exercise WebUI-owned reservation. Do not let the
    # operator's runtime-adapter selection turn them into runner-owner tests;
    # the dedicated runner test below overrides the predicate explicitly.
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")

    session_dir = tmp_path / "webui" / "sessions"
    session_dir.mkdir(parents=True)
    state_db = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    now = time.time() - 2_000
    messages = [
        {
            "id": 10,
            "role": "user",
            "content": "finish the atomic recovery work",
            "tool_calls": None,
            "tool_name": None,
            "tool_call_id": None,
            "timestamp": now,
            "finish_reason": None,
        }
    ]
    with sqlite3.connect(state_db) as con:
        con.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, source TEXT, model TEXT,
                started_at REAL, ended_at REAL, end_reason TEXT,
                cwd TEXT, git_repo_root TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                tool_calls TEXT, tool_name TEXT, tool_call_id TEXT,
                timestamp REAL, finish_reason TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
            ("session-1", "Atomic", "webui", "test-model", now - 10, None, None, str(workspace), None),
        )
        con.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
            (
                10,
                "session-1",
                "user",
                messages[0]["content"],
                None,
                None,
                None,
                now,
                None,
            ),
        )

    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(atomic_recovery, "_state_db_path", lambda _profile: state_db)
    config.SESSIONS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()

    session = models.Session(
        session_id="session-1",
        title="Atomic",
        workspace=str(workspace),
        model="test-model",
        messages=[{"role": "user", "content": messages[0]["content"], "timestamp": now}],
        active_stream_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
    )
    session.save(touch_updated_at=False)

    real_thread = threading.Thread

    class _NoWorkerThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            target = kwargs.get("target")
            self._suppressed = target in {routes._run_agent_streaming, routes._run_gateway_chat_streaming}
            self._delegate = None if self._suppressed else real_thread(*args, **kwargs)

        def start(self):
            if self._delegate is not None:
                return self._delegate.start()
            return None

        def join(self, timeout=None):
            if self._delegate is not None:
                return self._delegate.join(timeout=timeout)
            return None

        def is_alive(self):
            return bool(self._delegate and self._delegate.is_alive())

    monkeypatch.setattr(routes.threading, "Thread", _NoWorkerThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _config: False)
    monkeypatch.setattr(routes, "get_config", lambda: {})

    fingerprint = _transcript_hash(messages, 10)
    body = {
        "session_id": "session-1",
        "profile": "default",
        "source": "webui",
        "last_user_id": 10,
        "transcript_hash": fingerprint,
        "workspace": str(workspace),
        "recovery_claim_token": "claim-1",
    }

    yield {
        "atomic_recovery": atomic_recovery,
        "body": body,
        "config": config,
        "messages": messages,
        "models": models,
        "routes": routes,
        "session_dir": session_dir,
        "state_db": state_db,
        "workspace": workspace,
    }

    config.SESSIONS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()


def _reload_session(state):
    state["config"].SESSIONS.clear()
    return state["models"].Session.load("session-1")


def _write_sidecar(state, **changes):
    path = state["session_dir"] / "session-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")
    state["config"].SESSIONS.clear()


def _append_journal(state, event):
    from api.turn_journal import append_turn_journal_event

    return append_turn_journal_event("session-1", event, session_dir=state["session_dir"])


def test_internal_recovery_auth_requires_loopback_and_valid_durable_hmac(monkeypatch):
    from api import atomic_recovery

    key = b"k" * 32
    monkeypatch.setattr(atomic_recovery, "_signing_key", lambda: key)
    body = {
        "session_id": "session-1",
        "profile": "default",
        "source": "webui",
        "last_user_id": 10,
        "transcript_hash": "a" * 20,
        "workspace": "/tmp/workspace",
        "recovery_claim_token": "claim-1",
    }
    timestamp = str(int(time.time()))
    signature = hmac.new(
        key,
        atomic_recovery.internal_recovery_signing_bytes(body, timestamp),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-Hermes-Recovery-Timestamp": timestamp,
        "X-Hermes-Recovery-Signature": signature,
    }

    assert atomic_recovery.verify_internal_recovery_request(_Handler(headers=headers), body) == (True, None)
    allowed, reason = atomic_recovery.verify_internal_recovery_request(
        _Handler(address="192.168.1.5", headers=headers), body
    )
    assert allowed is False
    assert "loopback" in reason.lower()
    forged = dict(headers, **{"X-Hermes-Recovery-Signature": "0" * 64})
    allowed, reason = atomic_recovery.verify_internal_recovery_request(_Handler(headers=forged), body)
    assert allowed is False
    assert "authentication" in reason.lower()


def test_atomic_recovery_rejects_unsafe_session_id_before_filesystem_access(recovery_state):
    body = dict(recovery_state["body"], session_id="../../auth")
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(body)
    assert result["_status"] == 400
    assert "malformed" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}


@pytest.mark.parametrize("profile", ["", "../other", "other/profile", "Other", "other.name"])
def test_atomic_recovery_rejects_unsafe_or_missing_profile(recovery_state, profile):
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(
        dict(recovery_state["body"], profile=profile)
    )
    assert result["_status"] == 400
    assert "malformed" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}


def test_configured_recovery_db_is_bound_to_its_isolated_profile(monkeypatch, tmp_path):
    from api import atomic_recovery

    state_db = tmp_path / "named-state.db"
    named_home = tmp_path / "profiles" / "named"
    monkeypatch.setenv("HERMES_SESSION_WATCHDOG_DB", str(state_db))
    monkeypatch.setenv("HERMES_HOME", str(named_home))

    assert atomic_recovery._state_db_path("named") == state_db
    with pytest.raises(ValueError, match="does not own"):
        atomic_recovery._state_db_path("default")


def test_atomic_recovery_binds_profile_to_db_sidecar_and_loaded_session(recovery_state, monkeypatch):
    selected = []

    def profile_db(profile):
        selected.append(profile)
        return recovery_state["state_db"]

    monkeypatch.setattr(recovery_state["atomic_recovery"], "_state_db_path", profile_db)
    mismatched = dict(recovery_state["body"], profile="named")
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(mismatched)
    assert result["_status"] == 409
    assert "profile" in result["error"].lower()
    assert selected == ["named"]
    assert recovery_state["config"].STREAMS == {}

    _write_sidecar(recovery_state, profile="named")
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(mismatched)
    assert result.get("stream_id")
    assert result.get("_status", 200) == 200
    persisted = _reload_session(recovery_state)
    assert persisted.profile == "named"


def test_atomic_recovery_blocks_when_loaded_session_disappears(recovery_state, monkeypatch):
    monkeypatch.setattr(recovery_state["models"].Session, "load", classmethod(lambda _cls, _sid: None))
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "missing" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}


def test_atomic_recovery_uses_message_id_not_timestamp_for_latest_turn(recovery_state):
    with sqlite3.connect(recovery_state["state_db"]) as con:
        con.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
            (
                11,
                "session-1",
                "user",
                "newer logical turn with older clock",
                None,
                None,
                None,
                recovery_state["messages"][0]["timestamp"] - 10_000,
                None,
            ),
        )
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "logical turn" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}


def test_server_startup_materializes_durable_internal_recovery_key():
    server_source = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    assert "ensure_internal_recovery_key()" in server_source


@pytest.mark.parametrize(
    ("mutation", "reason_fragment"),
    [
        ({"active_stream_id": "live-stream", "pending_user_message": "still running", "pending_started_at": time.time()}, "fresh persisted"),
        ({"pending_user_message": "new human message", "pending_started_at": time.time()}, "fresh persisted"),
    ],
)
def test_atomic_recovery_blocks_active_and_pending_state(recovery_state, mutation, reason_fragment):
    _write_sidecar(recovery_state, **mutation)
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert reason_fragment in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}


def test_atomic_recovery_reconciles_only_matching_old_dead_sidecar_owner(recovery_state):
    old = time.time() - 4_000
    _write_sidecar(
        recovery_state,
        active_stream_id="dead-stream",
        pending_user_message=recovery_state["messages"][0]["content"],
        pending_started_at=old,
        pending_user_source="webui",
    )
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result.get("_status", 200) == 200
    persisted = _reload_session(recovery_state)
    assert persisted.pending_user_source == "cron-recovery"
    assert persisted.active_stream_id == result["stream_id"]


def test_atomic_recovery_stale_owner_save_failure_is_definitive_preflight_rejection(
    recovery_state, monkeypatch
):
    old = time.time() - 4_000
    _write_sidecar(
        recovery_state,
        active_stream_id="dead-stream",
        pending_user_message=recovery_state["messages"][0]["content"],
        pending_started_at=old,
        pending_user_source="webui",
    )
    models = recovery_state["models"]
    real_save = models.Session.save

    def fail_stale_owner_cleanup(session, *args, **kwargs):
        if session.active_stream_id is None and session.pending_user_source is None:
            raise OSError("sidecar cleanup unavailable")
        return real_save(session, *args, **kwargs)

    monkeypatch.setattr(models.Session, "save", fail_stale_owner_cleanup)
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "preflight" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}

    from api.turn_journal import read_turn_journal

    assert read_turn_journal("session-1", session_dir=recovery_state["session_dir"])["events"] == []


def test_atomic_recovery_post_reload_workspace_error_is_definitive_preflight_rejection(
    recovery_state, monkeypatch
):
    atomic_recovery = recovery_state["atomic_recovery"]
    real_resolve = atomic_recovery._resolved_existing_directory

    def fail_session_workspace(raw, label):
        if label == "session":
            raise OSError("workspace disappeared after reload")
        return real_resolve(raw, label)

    monkeypatch.setattr(atomic_recovery, "_resolved_existing_directory", fail_session_workspace)
    result = atomic_recovery.start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "preflight" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}

    from api.turn_journal import read_turn_journal

    assert read_turn_journal("session-1", session_dir=recovery_state["session_dir"])["events"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"pending_user_message": "different unsaved request", "pending_started_at": time.time() - 4_000},
        {
            "pending_user_message": "finish the atomic recovery work",
            "pending_started_at": time.time() - 4_000,
            "pending_attachments": [{"name": "unsaved.txt"}],
        },
    ],
)
def test_atomic_recovery_keeps_unreconciled_old_pending_state_fail_closed(recovery_state, mutation):
    _write_sidecar(recovery_state, **mutation)
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert recovery_state["config"].STREAMS == {}


def test_atomic_recovery_blocks_live_in_memory_owner_even_when_sidecar_is_old(recovery_state):
    _write_sidecar(
        recovery_state,
        active_stream_id="live-stream",
        pending_user_message=recovery_state["messages"][0]["content"],
        pending_started_at=time.time() - 4_000,
    )
    recovery_state["config"].STREAMS["live-stream"] = object()
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "live" in result["error"].lower()


def test_atomic_recovery_blocks_newer_turn_and_fingerprint_mismatch(recovery_state):
    with sqlite3.connect(recovery_state["state_db"]) as con:
        con.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
            (11, "session-1", "user", "new request", None, None, None, time.time(), None),
        )
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "logical turn" in result["error"].lower()

    with sqlite3.connect(recovery_state["state_db"]) as con:
        con.execute("DELETE FROM messages WHERE id = 11")
        con.execute("UPDATE messages SET content = ? WHERE id = 10", ("changed request",))
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "fingerprint" in result["error"].lower()


def test_atomic_recovery_rejects_model_and_qualified_provider_mismatch(recovery_state):
    _write_sidecar(recovery_state, model="different-model")
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "model mismatch" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}

    _write_sidecar(
        recovery_state,
        model="@custom:expected:test-model",
        model_provider="custom:other",
    )
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "provider mismatch" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}


def test_atomic_recovery_uses_db_model_with_consistent_qualified_sidecar(recovery_state, monkeypatch):
    _write_sidecar(
        recovery_state,
        model="@custom:expected:test-model",
        model_provider="custom:expected",
    )
    observed = {}

    def capture_start(_session, **kwargs):
        observed.update(kwargs)
        return {"session_id": "session-1", "stream_id": "stream", "turn_id": "turn"}

    monkeypatch.setattr(recovery_state["routes"], "_start_run", capture_start)
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["stream_id"] == "stream"
    assert observed["model"] == "test-model"
    assert observed["model_provider"] == "custom:expected"


@pytest.mark.parametrize("terminal", ["completed", "interrupted"])
def test_atomic_recovery_blocks_completed_or_interrupted_latest_turn(recovery_state, terminal):
    submitted = _append_journal(
        recovery_state,
        {
            "event": "submitted",
            "stream_id": "old-stream",
            "role": "user",
            "content": recovery_state["messages"][0]["content"],
            "created_at": recovery_state["messages"][0]["timestamp"],
        },
    )
    _append_journal(
        recovery_state,
        {
            "event": terminal,
            "stream_id": "old-stream",
            "turn_id": submitted["turn_id"],
            "created_at": time.time() - 100,
        },
    )
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert terminal in result["error"].lower()


def test_atomic_recovery_blocks_workspace_malformed_and_duplicate_state(recovery_state):
    mismatched = dict(recovery_state["body"], workspace=str(recovery_state["workspace"].parent))
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(mismatched)
    assert result["_status"] == 409
    assert "workspace" in result["error"].lower()

    sidecar = recovery_state["session_dir"] / "session-1.json"
    original = sidecar.read_text(encoding="utf-8")
    sidecar.write_text("{broken", encoding="utf-8")
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "malformed" in result["error"].lower()
    sidecar.write_text(original, encoding="utf-8")

    journal_dir = recovery_state["session_dir"] / "_turn_journal"
    journal_dir.mkdir(exist_ok=True)
    malformed = journal_dir / "session-1.jsonl"
    malformed.write_text("{broken\n", encoding="utf-8")
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "malformed" in result["error"].lower()
    malformed.unlink()

    _append_journal(
        recovery_state,
        {
            "event": "submitted",
            "source": "cron-recovery",
            "recovery_claim_token": "claim-1",
            "recovery_fingerprint": recovery_state["body"]["transcript_hash"],
        },
    )
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "duplicate" in result["error"].lower()


def test_atomic_recovery_launch_failed_does_not_close_possible_worker(recovery_state):
    reservation = _append_journal(
        recovery_state,
        {
            "event": "submitted",
            "stream_id": "old-stream",
            "source": "cron-recovery",
            "recovery_claim_token": "claim-1",
            "recovery_fingerprint": recovery_state["body"]["transcript_hash"],
        },
    )
    for event_name in ("launch_failed", "worker_started"):
        _append_journal(
            recovery_state,
            {
                "event": event_name,
                "stream_id": "old-stream",
                "turn_id": reservation["turn_id"],
                "source": "cron-recovery",
                "recovery_claim_token": "claim-1",
                "recovery_fingerprint": recovery_state["body"]["transcript_hash"],
            },
        )
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert "duplicate" in result["error"].lower()


@pytest.mark.parametrize("terminal", ["completed", "interrupted"])
def test_atomic_recovery_terminal_binding_ignores_clock_skew(recovery_state, terminal):
    submitted = _append_journal(
        recovery_state,
        {
            "event": "submitted",
            "stream_id": "old-stream",
            "role": "user",
            "content": recovery_state["messages"][0]["content"],
            "created_at": recovery_state["messages"][0]["timestamp"] + 10_000,
        },
    )
    _append_journal(
        recovery_state,
        {
            "event": terminal,
            "stream_id": "old-stream",
            "turn_id": submitted["turn_id"],
            "created_at": recovery_state["messages"][0]["timestamp"] - 10_000,
        },
    )
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 409
    assert terminal in result["error"].lower()


@pytest.mark.parametrize(
    ("request_has_git_root", "db_has_git_root"),
    [(False, True), (True, False)],
)
def test_atomic_recovery_requires_repository_root_presence_to_match(
    recovery_state, request_has_git_root, db_has_git_root
):
    repo_root = recovery_state["workspace"].parent / "repo"
    repo_root.mkdir()
    with sqlite3.connect(recovery_state["state_db"]) as con:
        con.execute(
            "UPDATE sessions SET git_repo_root = ? WHERE id = ?",
            (str(repo_root) if db_has_git_root else None, "session-1"),
        )
    body = dict(recovery_state["body"])
    if request_has_git_root:
        body["git_repo_root"] = str(repo_root)

    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(body)

    assert result["_status"] == 409
    assert "repository mismatch" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}


def test_human_vs_recovery_race_has_exactly_one_winner_and_expected_start(recovery_state, monkeypatch):
    atomic_recovery = recovery_state["atomic_recovery"]
    routes = recovery_state["routes"]
    original_start_run = routes._start_run
    recovery_entered_start = threading.Event()
    human_attempting = threading.Event()
    thread_timeout = 15.0
    observed = {}

    def recovery_start_run(session, **kwargs):
        lock = recovery_state["config"]._get_session_agent_lock(session.session_id)
        observed["lock_held"] = not lock.acquire(blocking=False)
        if not observed["lock_held"]:
            lock.release()
        observed["session_id"] = session.session_id
        observed["workspace"] = kwargs["workspace"]
        recovery_entered_start.set()
        assert human_attempting.wait(timeout=thread_timeout), "human runner did not reach the session lock"
        return original_start_run(session, **kwargs)

    monkeypatch.setattr(routes, "_start_run", recovery_start_run)
    # Reproduce the real handler race: the human request may resolve/cache the
    # Session object before recovery acquires ownership. The recovery path must
    # not replace that object and leave the human handler with stale run state.
    human_session = recovery_state["models"].get_session("session-1")
    outcomes = {}

    def recovery_runner():
        outcomes["recovery"] = atomic_recovery.start_atomic_webui_recovery(recovery_state["body"])

    def human_runner():
        assert recovery_entered_start.wait(timeout=thread_timeout), "recovery runner did not reach _start_run"
        human_attempting.set()
        outcomes["human"] = original_start_run(
            human_session,
            msg="human follow-up",
            attachments=[],
            workspace=str(recovery_state["workspace"]),
            model="test-model",
            model_provider=None,
            normalized_model=False,
            source="webui",
            route="test-human",
        )

    recovery_thread = threading.Thread(target=recovery_runner)
    human_thread = threading.Thread(target=human_runner)
    recovery_thread.start()
    human_thread.start()
    recovery_thread.join(timeout=thread_timeout * 2)
    human_thread.join(timeout=thread_timeout * 2)

    assert not recovery_thread.is_alive(), "recovery runner did not finish"
    assert not human_thread.is_alive(), "human runner did not finish"
    statuses = sorted(int(result.get("_status", 200)) for result in outcomes.values())
    assert statuses == [200, 409]
    assert outcomes["recovery"].get("stream_id")
    assert outcomes["human"]["error"] == "session already has an active stream"
    assert observed == {
        "lock_held": True,
        "session_id": "session-1",
        "workspace": str(recovery_state["workspace"]),
    }
    persisted = _reload_session(recovery_state)
    assert persisted.pending_user_source == "cron-recovery"
    assert persisted.workspace == str(recovery_state["workspace"])
    from api.turn_journal import read_turn_journal

    events = read_turn_journal("session-1", session_dir=recovery_state["session_dir"])["events"]
    reservation = next(event for event in events if event.get("source") == "cron-recovery")
    assert reservation["profile"] == "default"
    assert reservation["recovery_assistant_index_before"] == -1
    assert reservation["recovery_claim_token"] == "claim-1"


def test_atomic_recovery_never_launches_without_durable_reservation(recovery_state, monkeypatch):
    from api import turn_journal

    def fail_append(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(turn_journal, "append_turn_journal_event", fail_append)
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 500
    assert "journaled" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}
    persisted = _reload_session(recovery_state)
    assert persisted.active_stream_id == result["active_stream_id"]
    assert persisted.pending_user_source == "cron-recovery"


def test_atomic_recovery_durable_launch_failure_cleans_owner_and_allows_retry(recovery_state, monkeypatch):
    routes = recovery_state["routes"]

    class _LaunchFailureThread:
        ident = None

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

        def is_alive(self):
            return False

    monkeypatch.setattr(routes.threading, "Thread", _LaunchFailureThread)

    first = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert first["_status"] == 409
    assert first["recovery_launch_failed"] is True
    assert recovery_state["config"].STREAMS == {}
    persisted = _reload_session(recovery_state)
    assert persisted.active_stream_id is None
    assert persisted.pending_user_message is None
    assert persisted.pending_user_source is None

    from api.turn_journal import read_turn_journal

    events = read_turn_journal("session-1", session_dir=recovery_state["session_dir"])["events"]
    submitted = [
        event
        for event in events
        if event.get("source") == "cron-recovery" and event.get("event") == "submitted"
    ]
    failed = [event for event in events if event.get("event") == "launch_failed"]
    assert len(submitted) == len(failed) == 1
    assert failed[0]["turn_id"] == submitted[0]["turn_id"]
    assert failed[0]["stream_id"] == submitted[0]["stream_id"]

    second = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert second["_status"] == 409
    assert second["recovery_launch_failed"] is True
    assert "duplicate" not in second["error"].lower()


def test_atomic_recovery_cleanup_save_failure_stays_uncertain(recovery_state, monkeypatch):
    routes = recovery_state["routes"]
    models = recovery_state["models"]
    real_save = models.Session.save

    class _LaunchFailureThread:
        ident = None

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

        def is_alive(self):
            return False

    def fail_cleanup_save(session, *args, **kwargs):
        if session.active_stream_id is None and session.pending_user_source is None:
            raise OSError("sidecar cleanup unavailable")
        return real_save(session, *args, **kwargs)

    monkeypatch.setattr(routes.threading, "Thread", _LaunchFailureThread)
    monkeypatch.setattr(models.Session, "save", fail_cleanup_save)
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 500
    assert "cleanup is uncertain" in result["error"].lower()

    from api.turn_journal import read_turn_journal

    events = read_turn_journal("session-1", session_dir=recovery_state["session_dir"])["events"]
    assert not any(event.get("event") == "launch_failed" for event in events)
    persisted = _reload_session(recovery_state)
    assert persisted.active_stream_id == result["active_stream_id"]
    assert persisted.pending_user_source == "cron-recovery"


def test_atomic_recovery_launch_failure_journal_error_stays_uncertain(recovery_state, monkeypatch):
    from api import turn_journal

    routes = recovery_state["routes"]

    class _LaunchFailureThread:
        ident = None

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

        def is_alive(self):
            return False

    def fail_launch_failure_append(*_args, **_kwargs):
        raise OSError("launch failure journal unavailable")

    monkeypatch.setattr(routes.threading, "Thread", _LaunchFailureThread)
    monkeypatch.setattr(turn_journal, "append_turn_journal_event_for_stream", fail_launch_failure_append)
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 500
    assert "could not be journaled" in result["error"].lower()
    persisted = _reload_session(recovery_state)
    assert persisted.active_stream_id is None
    assert persisted.pending_user_source is None

    events = turn_journal.read_turn_journal(
        "session-1", session_dir=recovery_state["session_dir"]
    )["events"]
    assert not any(event.get("event") == "launch_failed" for event in events)
    retry = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert retry["_status"] == 409
    assert "duplicate" in retry["error"].lower()


def test_atomic_recovery_uncertain_launch_keeps_owner_and_reservation(recovery_state, monkeypatch):
    routes = recovery_state["routes"]

    class _UncertainLaunchThread:
        ident = 123

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("raised after possible launch")

        def is_alive(self):
            return False

    monkeypatch.setattr(routes.threading, "Thread", _UncertainLaunchThread)
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 500
    assert "uncertain" in result["error"].lower()
    persisted = _reload_session(recovery_state)
    assert persisted.active_stream_id == result["active_stream_id"]
    assert persisted.pending_user_source == "cron-recovery"

    from api.turn_journal import read_turn_journal

    events = read_turn_journal("session-1", session_dir=recovery_state["session_dir"])["events"]
    assert not any(event.get("event") == "launch_failed" for event in events)


def test_atomic_recovery_fails_closed_for_gateway_owner_before_reservation(recovery_state, monkeypatch):
    from api import gateway_chat

    monkeypatch.setattr(gateway_chat, "webui_gateway_chat_enabled", lambda: True)
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 501
    assert "gateway" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}
    persisted = _reload_session(recovery_state)
    assert persisted.active_stream_id is None
    from api.turn_journal import read_turn_journal

    assert read_turn_journal("session-1", session_dir=recovery_state["session_dir"])["events"] == []


def test_atomic_recovery_fails_closed_for_external_runner_owner(recovery_state, monkeypatch):
    from api import runtime_adapter

    monkeypatch.setattr(runtime_adapter, "runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_runner_enabled", lambda: True)
    result = recovery_state["atomic_recovery"].start_atomic_webui_recovery(recovery_state["body"])
    assert result["_status"] == 501
    assert "atomic recovery" in result["error"].lower()
    assert recovery_state["config"].STREAMS == {}
