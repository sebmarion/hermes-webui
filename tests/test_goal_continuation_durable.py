"""Durability and ownership contract for automatic WebUI goal turns."""

import json
import os

import pytest

from api import config
import api.goal_continuation as goal_continuation
import api.streaming as streaming


@pytest.fixture
def receipt_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(
        goal_continuation,
        "_goal_revision_is_active",
        lambda _sid, revision, profile_home=None: int(revision) == 7,
    )
    return session_dir


def _claim(**overrides):
    values = {
        "session_id": "goal-session",
        "parent_run_id": "parent-run",
        "prompt": "continue the standing goal",
        "goal_revision": 7,
        "profile_home": "/tmp/profile-home",
    }
    values.update(overrides)
    return goal_continuation.claim_goal_continuation(**values)


def test_duplicate_claim_and_settle_starts_one_successor(receipt_store):
    first = _claim()
    second = _claim()
    starts = []

    def start(sid, prompt):
        starts.append((sid, prompt))
        return {"stream_id": "successor-run", "session_id": sid}

    settled = goal_continuation.settle_goal_continuation(
        "goal-session", "parent-run", start=start
    )
    duplicate = goal_continuation.settle_goal_continuation(
        "goal-session", "parent-run", start=start
    )

    assert first["claim_key"] == second["claim_key"]
    assert settled["state"] == duplicate["state"] == "started"
    assert settled["child_stream_id"] == "successor-run"
    assert starts == [("goal-session", "continue the standing goal")]


def test_failed_start_remains_durable_and_restart_recovery_is_exactly_once(receipt_store):
    _claim()

    failed = goal_continuation.settle_goal_continuation(
        "goal-session",
        "parent-run",
        start=lambda _sid, _prompt: {"error": "busy", "_status": 409},
    )
    assert failed["state"] == "claimed"

    starts = []
    recovered = goal_continuation.recover_pending_goal_continuations(
        start=lambda sid, prompt: starts.append((sid, prompt))
        or {"stream_id": "recovered-run", "session_id": sid}
    )
    repeated = goal_continuation.recover_pending_goal_continuations(
        start=lambda sid, prompt: starts.append((sid, prompt))
        or {"stream_id": "duplicate-run"}
    )

    assert recovered == 1
    assert repeated == 0
    assert starts == [("goal-session", "continue the standing goal")]


def test_start_response_for_different_session_remains_retryable(receipt_store):
    _claim()

    mismatched = goal_continuation.settle_goal_continuation(
        "goal-session",
        "parent-run",
        start=lambda _sid, _prompt: {
            "stream_id": "wrong-session-run",
            "session_id": "different-session",
        },
    )

    assert mismatched["state"] == "claimed"
    assert "child_stream_id" not in mismatched


def test_stale_goal_revision_is_discarded_fail_closed(receipt_store, monkeypatch):
    _claim(goal_revision=8)
    starts = []

    settled = goal_continuation.settle_goal_continuation(
        "goal-session",
        "parent-run",
        start=lambda sid, prompt: starts.append((sid, prompt)) or {"stream_id": "bad"},
    )

    assert settled["state"] == "discarded"
    assert settled["discarded_reason"] == "stale_goal_revision"
    assert starts == []


def test_restart_reclaims_starting_receipt_owned_by_dead_process(receipt_store, monkeypatch):
    claimed = _claim()
    path = receipt_store / "_goal_continuations.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    row = store["receipts"][claimed["claim_key"]]
    row["state"] = "starting"
    row["owner_pid"] = os.getpid() + 1_000_000
    path.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(goal_continuation, "_pid_is_alive", lambda _pid: False)
    starts = []

    recovered = goal_continuation.recover_pending_goal_continuations(
        start=lambda sid, prompt: starts.append((sid, prompt))
        or {"stream_id": "reclaimed-run", "session_id": sid}
    )

    assert recovered == 1
    assert starts == [("goal-session", "continue the standing goal")]


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps({"version": 999, "receipts": {}}),
        json.dumps({"version": 1, "receipts": []}),
    ],
)
def test_receipt_store_corruption_fails_closed_without_overwrite(receipt_store, raw):
    path = receipt_store / "_goal_continuations.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(RuntimeError, match="goal continuation receipt"):
        _claim(parent_run_id="corrupt-parent")

    assert path.read_text(encoding="utf-8") == raw


def test_receipt_never_persists_profile_secrets(receipt_store):
    receipt = _claim()

    assert receipt["profile_home"] == "/tmp/profile-home"
    serialized = (receipt_store / "_goal_continuations.json").read_text(encoding="utf-8")
    assert "api_key" not in serialized.lower()
    assert "token" not in serialized.lower()


def test_goal_control_prompt_is_model_context_only():
    prompt = "[Continuing toward your standing goal] hidden control prompt"
    merged = streaming._merge_display_messages_after_agent_result(
        [],
        [],
        [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "visible progress"},
        ],
        prompt,
        source=goal_continuation.SOURCE,
    )

    assert merged == [{"role": "assistant", "content": "visible progress"}]


def test_hidden_goal_control_does_not_turn_valid_answer_into_no_response():
    prompt = "[Continuing toward your standing goal] hidden control prompt"
    previous_display = [{"role": "assistant", "content": "prior progress"}]
    previous_context = list(previous_display)
    result = previous_context + [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "finished goal step"},
    ]

    assert not streaming._merged_transcript_lacks_final_assistant_answer(
        previous_display,
        previous_context,
        result,
        prompt,
        source=goal_continuation.SOURCE,
    )


def test_hidden_goal_control_still_detects_genuinely_empty_answer():
    prompt = "[Continuing toward your standing goal] hidden control prompt"
    previous = [{"role": "assistant", "content": "prior progress"}]

    assert streaming._merged_transcript_lacks_final_assistant_answer(
        previous,
        previous,
        previous + [{"role": "user", "content": prompt}],
        prompt,
        source=goal_continuation.SOURCE,
    )
