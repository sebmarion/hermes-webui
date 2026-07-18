import json

import pytest

from api.bounded_runtime_overlay import (
    VERIFIED_RUNTIME_OVERLAY_CAPABILITY,
    RuntimeOwner,
    assemble_runtime_overlay,
)
from api.run_journal import append_run_event


def _owner(**overrides):
    values = {
        "profile": "default",
        "session_id": "session_1",
        "run_id": "run_1",
        "active": True,
        "capability_token": "owner-epoch-1",
    }
    values.update(overrides)
    return RuntimeOwner(**values)


def _verified(owner):
    return lambda candidate: candidate == owner


def _message(message_id, content):
    return {"_state_db_message_id": message_id, "role": "assistant", "content": content}


def test_proven_active_owner_overlays_one_matching_journal_run(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": " live"}, session_dir=tmp_path)
    owner = _owner()
    result = assemble_runtime_overlay(
        [_message(1, "settled")], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner),
        session_dir=tmp_path,
        in_memory_messages=[{"_runtime_message_id": "run_1:live", "role": "assistant", "content": "live"}],
        pending_user_message="next question",
    )
    assert result.status == "ok"
    assert [message["content"] for message in result.messages] == ["settled", "next question", "live"]
    assert result.messages[1]["_runtime_message_id"] == "run_1:pending-user"
    assert result.pending_user_message == "next question"
    assert result.journal_events[0]["event_id"] == "run_1:1"
    assert result.capability_marker is VERIFIED_RUNTIME_OVERLAY_CAPABILITY


def test_proven_active_owner_projects_the_bounded_journal_without_live_buffers(
    tmp_path,
):
    append_run_event(
        "session_1",
        "run_1",
        "reasoning",
        {"text": "considering"},
        session_dir=tmp_path,
    )
    append_run_event(
        "session_1",
        "run_1",
        "token",
        {"text": "answer"},
        session_dir=tmp_path,
    )
    append_run_event(
        "session_1",
        "run_1",
        "tool",
        {"name": "search", "id": "call-1", "args": {"q": "bounded"}},
        session_dir=tmp_path,
    )
    append_run_event(
        "session_1",
        "run_1",
        "tool_complete",
        {"name": "search", "id": "call-1", "preview": "done"},
        session_dir=tmp_path,
    )
    owner = _owner()

    result = assemble_runtime_overlay(
        [_message(1, "settled")],
        profile="default",
        session_id="session_1",
        owner=owner,
        owner_verifier=_verified(owner),
        session_dir=tmp_path,
    )

    assert result.status == "ok"
    assert [message["content"] for message in result.messages] == [
        "settled",
        "answer",
    ]
    live = result.messages[-1]
    assert live["_runtime_message_id"] == "run_1:assistant"
    assert live["reasoning"] == "considering"
    assert live["_partial_tool_calls"] == [
        {
            "args": {"q": "bounded"},
            "done": True,
            "id": "call-1",
            "is_error": False,
            "name": "search",
            "preview": "done",
        }
    ]


def test_verified_live_buffer_supersedes_same_run_journal_projection(tmp_path):
    append_run_event(
        "session_1",
        "run_1",
        "token",
        {"text": "older"},
        session_dir=tmp_path,
    )
    owner = _owner()

    result = assemble_runtime_overlay(
        [],
        profile="default",
        session_id="session_1",
        owner=owner,
        owner_verifier=_verified(owner),
        session_dir=tmp_path,
        in_memory_messages=[
            {
                "_runtime_message_id": "run_1:assistant",
                "role": "assistant",
                "content": "newer",
            }
        ],
    )

    assert [message["content"] for message in result.messages] == ["newer"]


def test_overlay_fails_closed_for_owner_mismatch_and_never_mutates_settled_messages(tmp_path):
    settled = [_message(1, "settled")]
    owner = _owner()
    result = assemble_runtime_overlay(
        settled, profile="other", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path
    )
    assert result.status == "runtime_owner_profile_mismatch"
    assert result.messages == settled
    assert result.messages is not settled


@pytest.mark.parametrize("raw", ["not json\n", "[]\n", "42\n", '"scalar"\n'])
def test_overlay_rejects_malformed_json_value_with_typed_status(tmp_path, raw):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    owner = _owner()
    malformed = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path
    )
    assert malformed.status == "runtime_journal_malformed"


def test_overlay_enforces_hard_journal_row_and_byte_budgets(tmp_path):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"version":1,"event_id":"run_1:1","seq":1,"run_id":"run_1","session_id":"session_1","event":"token","type":"token","created_at":1.0,"terminal":false,"terminal_state":null,"payload":{}}\n',
        encoding="utf-8",
    )
    owner = _owner()
    rows = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path, max_rows=0
    )
    bytes_limited = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path, max_bytes=1
    )
    assert rows.status == "runtime_journal_limit_rows"
    assert bytes_limited.status == "runtime_journal_limit_bytes"


def test_overlay_deduplicates_stable_identity_and_leaves_canonical_metadata_unchanged(tmp_path):
    settled = [
        {
            "_state_db_message_id": 1,
            "_runtime_message_id": "run_1:already-settled",
            "role": "assistant",
            "content": "settled",
        }
    ]
    owner = _owner()
    result = assemble_runtime_overlay(
        settled, profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path,
        in_memory_messages=[
            {
                "_state_db_message_id": 1,
                "_runtime_message_id": "run_1:already-settled",
                "role": "assistant",
                "content": "duplicate",
            },
            {
                "message_id": 999,
                "_runtime_message_id": "run_1:already-settled",
                "role": "assistant",
                "content": "duplicate by secondary identity",
            },
            {"_runtime_message_id": "run_1:2", "role": "assistant", "content": "live"},
        ],
    )
    assert [message["content"] for message in result.messages] == ["settled", "live"]
    assert settled[0]["content"] == "settled"


def test_active_boolean_without_registry_verification_is_not_owner_proof(tmp_path):
    result = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=_owner(), session_dir=tmp_path
    )
    assert result.status == "runtime_owner_unverified"


def test_owner_rotation_and_all_owner_mismatches_fail_closed(tmp_path):
    owner = _owner()
    rotated = _owner(capability_token="owner-epoch-2")

    assert assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=None, session_dir=tmp_path
    ).status == "no_active_owner"
    assert assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=_owner(active=False),
        owner_verifier=_verified(owner), session_dir=tmp_path
    ).status == "no_active_owner"
    non_boolean_active = _owner(active=1)
    assert assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=non_boolean_active,
        owner_verifier=_verified(non_boolean_active), session_dir=tmp_path
    ).status == "no_active_owner"
    assert assemble_runtime_overlay(
        [], profile="other", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path
    ).status == "runtime_owner_profile_mismatch"
    assert assemble_runtime_overlay(
        [], profile="default", session_id="session_2", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path
    ).status == "runtime_owner_session_mismatch"
    assert assemble_runtime_overlay(
        [], profile="default", session_id="session_1", run_id="run_2", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path
    ).status == "runtime_owner_run_mismatch"
    assert assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(rotated), session_dir=tmp_path
    ).status == "runtime_owner_unverified"


def test_owner_rotation_after_journal_read_discards_the_whole_overlay(tmp_path):
    owner = _owner()
    verifications = iter((True, False))

    result = assemble_runtime_overlay(
        [_message(1, "settled")], profile="default", session_id="session_1", owner=owner,
        owner_verifier=lambda _candidate: next(verifications), session_dir=tmp_path,
        in_memory_messages=[
            {"_runtime_message_id": "run_1:live", "role": "assistant", "content": "live"}
        ],
    )

    assert result.status == "runtime_owner_rotated"
    assert result.messages == [_message(1, "settled")]
    assert result.journal_events == []


@pytest.mark.parametrize("field,value", [("max_rows", 1.0), ("max_rows", True), ("max_bytes", "10"), ("max_bytes", -1)])
def test_limits_require_non_negative_plain_integers(tmp_path, field, value):
    owner = _owner()
    result = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path, **{field: value}
    )
    assert result.status == "runtime_limit_invalid"


def test_journal_oserror_is_typed_degradation(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("api.bounded_runtime_overlay._iter_bounded_raw_jsonl_lines", denied)
    owner = _owner()
    result = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path
    )
    assert result.status == "runtime_journal_io_error"


def test_in_memory_rows_and_total_serialized_bytes_are_bounded(tmp_path):
    owner = _owner()
    messages = [
        {"_runtime_message_id": "run_1:1", "role": "assistant", "content": "one"},
        {"_runtime_message_id": "run_1:2", "role": "assistant", "content": "two"},
    ]
    rows = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path,
        in_memory_messages=messages, max_rows=1,
    )
    assert rows.status == "runtime_overlay_limit_rows"

    pending = {
        "_runtime_message_id": "run_1:pending-user",
        "role": "user",
        "content": "question",
        "_pending": True,
        "_live": True,
    }
    pending_bytes = len(json.dumps(pending, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    limited = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path,
        pending_user_message="question", max_bytes=pending_bytes - 1,
    )
    assert limited.status == "runtime_overlay_limit_bytes"


def test_runtime_and_pending_identities_are_bound_to_owner_run(tmp_path):
    owner = _owner()
    wrong_runtime = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path,
        in_memory_messages=[
            {
                "_state_db_message_id": 1,
                "_runtime_message_id": "run_2:1",
                "role": "assistant",
                "content": "foreign",
            }
        ],
    )
    wrong_pending = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path,
        pending_user_message="question", pending_user_message_id="run_2:pending-user",
    )
    assert wrong_runtime.status == "runtime_message_run_mismatch"
    assert wrong_pending.status == "runtime_message_run_mismatch"


@pytest.mark.parametrize("pending_id", ["", "run_1", "run_1:"])
def test_pending_identity_must_be_a_complete_owner_bound_runtime_id(tmp_path, pending_id):
    owner = _owner()
    result = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path,
        pending_user_message="question", pending_user_message_id=pending_id,
    )
    assert result.status == "runtime_message_run_mismatch"


def test_pending_turn_is_owner_bound_ordered_before_assistant_and_deduped_when_settled(tmp_path):
    owner = _owner()
    assistant = {"_runtime_message_id": "run_1:assistant", "role": "assistant", "content": "answer"}
    result = assemble_runtime_overlay(
        [_message(1, "settled")], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path,
        in_memory_messages=[assistant], pending_user_message="question",
    )
    assert [(m["role"], m["content"]) for m in result.messages] == [
        ("assistant", "settled"), ("user", "question"), ("assistant", "answer")
    ]

    settled_pending = result.messages[1]
    deduped = assemble_runtime_overlay(
        [settled_pending], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path, pending_user_message="question",
    )
    assert deduped.messages == [settled_pending]


def test_journal_record_for_another_run_or_session_is_rejected(tmp_path):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"event_id":"run_2:1","seq":1,"run_id":"run_2","session_id":"session_1","event":"token","payload":{}}\n',
        encoding="utf-8",
    )
    owner = _owner()
    result = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path,
    )
    assert result.status == "runtime_journal_owner_mismatch"


@pytest.mark.parametrize(
    "record",
    [
        {"version": 1, "seq": True},
        {"version": 1, "seq": 1.0},
        {"version": 2, "seq": 1},
        {"version": 1, "seq": 1, "event": "token", "type": "different"},
    ],
)
def test_journal_requires_strict_version_sequence_and_event_shape(tmp_path, record):
    complete = {
        "version": 1,
        "event_id": "run_1:1",
        "seq": 1,
        "run_id": "run_1",
        "session_id": "session_1",
        "event": "token",
        "type": "token",
        "created_at": 1.0,
        "terminal": False,
        "terminal_state": None,
        "payload": {},
    }
    complete.update(record)
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(complete) + "\n", encoding="utf-8")
    owner = _owner()

    result = assemble_runtime_overlay(
        [],
        profile="default",
        session_id="session_1",
        owner=owner,
        owner_verifier=_verified(owner),
        session_dir=tmp_path,
    )

    assert result.status == "runtime_journal_malformed"


def test_owner_capability_pending_and_runtime_json_must_be_strict(tmp_path):
    owner = _owner(capability_token=None)
    assert assemble_runtime_overlay(
        [],
        profile="default",
        session_id="session_1",
        owner=owner,
        owner_verifier=_verified(owner),
        session_dir=tmp_path,
    ).status == "runtime_owner_unverified"

    owner = _owner()
    assert assemble_runtime_overlay(
        [],
        profile="default",
        session_id="session_1",
        owner=owner,
        owner_verifier=_verified(owner),
        session_dir=tmp_path,
        pending_user_message={"not": "text"},
    ).status == "runtime_message_malformed"
    assert assemble_runtime_overlay(
        [],
        profile="default",
        session_id="session_1",
        owner=owner,
        owner_verifier=_verified(owner),
        session_dir=tmp_path,
        in_memory_messages=[
            {
                "_runtime_message_id": "run_1:live",
                "role": "assistant",
                "content": float("nan"),
            }
        ],
    ).status == "runtime_message_malformed"


@pytest.mark.parametrize(
    "mutation",
    [
        {"version": True},
        {"created_at": float("nan")},
        {"terminal": 0},
        {"terminal_state": "completed"},
        {"profile": "default"},
    ],
)
def test_journal_record_requires_the_exact_finite_append_schema(tmp_path, mutation):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    record = {
        "version": 1,
        "event_id": "run_1:1",
        "seq": 1,
        "run_id": "run_1",
        "session_id": "session_1",
        "event": "token",
        "type": "token",
        "created_at": 1.0,
        "terminal": False,
        "terminal_state": None,
        "payload": {},
    }
    record.update(mutation)
    path.write_text(json.dumps(record), encoding="utf-8")

    result = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=_owner(),
        owner_verifier=_verified(_owner()), session_dir=tmp_path,
    )

    assert result.status == "runtime_journal_malformed"


def test_journal_rejects_created_at_too_large_to_convert_to_float(tmp_path):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    record = {
        "version": 1,
        "event_id": "run_1:1",
        "seq": 1,
        "run_id": "run_1",
        "session_id": "session_1",
        "event": "token",
        "type": "token",
        "created_at": 10**4000,
        "terminal": False,
        "terminal_state": None,
        "payload": {},
    }
    path.write_text(json.dumps(record), encoding="utf-8")

    result = assemble_runtime_overlay(
        [], profile="default", session_id="session_1", owner=_owner(),
        owner_verifier=_verified(_owner()), session_dir=tmp_path,
    )

    assert result.status == "runtime_journal_malformed"


def test_owner_binding_rejects_type_coercion(tmp_path):
    owner = _owner(profile=1, session_id=1, run_id=1)
    result = assemble_runtime_overlay(
        [], profile="1", session_id="1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path,
    )
    assert result.status == "runtime_owner_invalid"


def test_deeply_nested_journal_payload_fails_closed_without_recursion_error(tmp_path):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    record = {
        "version": 1,
        "event_id": "run_1:1",
        "seq": 1,
        "run_id": "run_1",
        "session_id": "session_1",
        "event": "token",
        "type": "token",
        "created_at": 1.0,
        "terminal": False,
        "terminal_state": None,
        "payload": None,
    }
    encoded = json.dumps(record, separators=(",", ":"))
    deeply_nested = ("[" * 1_200) + "0" + ("]" * 1_200)
    path.write_text(
        encoded.replace('"payload":null', f'"payload":{deeply_nested}') + "\n",
        encoding="utf-8",
    )

    result = assemble_runtime_overlay(
        [],
        profile="default",
        session_id="session_1",
        owner=_owner(),
        owner_verifier=_verified(_owner()),
        session_dir=tmp_path,
    )

    assert result.status == "runtime_journal_malformed"


def test_duplicate_in_memory_record_must_still_have_a_run_bound_identity(tmp_path):
    owner = _owner()
    result = assemble_runtime_overlay(
        [_message(1, "settled")], profile="default", session_id="session_1", owner=owner,
        owner_verifier=_verified(owner), session_dir=tmp_path,
        in_memory_messages=[
            {"_state_db_message_id": 1, "role": "assistant", "content": "bad duplicate"}
        ],
    )
    assert result.status == "runtime_message_identity_missing"
