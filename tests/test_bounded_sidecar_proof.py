"""Fail-closed proof coverage for bounded WebUI sidecar metadata reads."""
from __future__ import annotations

import json
import math
import os

import pytest

from api.bounded_sidecar_proof import (
    MAX_METADATA_PREFIX_BYTES,
    SidecarProofInputError,
    prove_sidecar,
    prove_sidecar_lineage,
)


def _write_sidecar(directory, session_id="session", *, file_session_id=None, **overrides):
    payload = {
        "session_id": session_id,
        "profile": "default",
        "sidecar_generation": 4,
        "truncation_watermark": 12.5,
        "title": "Compact title",
        "workspace": "/workspace",
        "model": "test-model",
        "created_at": 1.0,
        "updated_at": 2.0,
        "pinned": False,
        "archived": False,
        "message_count": 9,
        "messages": [{"role": "user", "content": "must never be returned"}],
        "tool_calls": [{"secret": "must never be returned"}],
        "anchor_activity_scenes": {"scene": {"body": "must never be returned"}},
    }
    payload.update(overrides)
    path = directory / f"{file_session_id or session_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_present_sidecar_returns_only_compact_route_metadata_and_exact_signature(tmp_path):
    _write_sidecar(tmp_path)

    proof = prove_sidecar(tmp_path, "session", "default")

    assert proof.status == "present"
    assert proof.diagnostic == "ok"
    assert proof.session_id == "session"
    assert proof.sidecar_generation == 4
    assert proof.truncation_watermark == 12.5
    assert proof.stat_signature is not None
    assert proof.stat_signature.inode == os.stat(tmp_path / "session.json").st_ino
    assert proof.route_metadata == {
        "session_id": "session",
        "profile": "default",
        "title": "Compact title",
        "workspace": "/workspace",
        "model": "test-model",
        "model_provider": None,
        "created_at": 1.0,
        "updated_at": 2.0,
        "pinned": False,
        "archived": False,
        "project_id": None,
        "parent_session_id": None,
        "session_source": None,
        "source_tag": None,
        "source_label": None,
        "is_cli_session": False,
        "read_only": False,
        "message_count": 9,
    }
    assert "messages" not in proof.route_metadata
    assert "tool_calls" not in proof.route_metadata
    assert "anchor_activity_scenes" not in proof.route_metadata
    assert "/" not in proof.diagnostic


def test_oversized_prefix_fails_closed_without_reading_messages(tmp_path):
    (tmp_path / "session.json").write_bytes(
        b'{"session_id":"session","profile":"default","sidecar_generation":4,"padding":"'
        + (b"x" * MAX_METADATA_PREFIX_BYTES)
    )

    proof = prove_sidecar(tmp_path, "session", "default")

    assert proof.status == "invalid"
    assert proof.diagnostic == "metadata_prefix_too_large"


def test_truncated_metadata_prefix_is_not_promoted_to_a_proof(tmp_path):
    (tmp_path / "session.json").write_text(
        '{"session_id":"session","profile":"default","sidecar_generation":4,',
        encoding="utf-8",
    )

    proof = prove_sidecar(tmp_path, "session", "default")

    assert proof.status == "invalid"
    assert proof.diagnostic == "metadata_prefix_malformed"


def test_malformed_json_prefix_is_not_promoted_to_a_proof(tmp_path):
    (tmp_path / "session.json").write_text(
        '{"session_id":"session" "profile":"default","sidecar_generation":4,"messages":[]}',
        encoding="utf-8",
    )

    proof = prove_sidecar(tmp_path, "session", "default")

    assert proof.status == "invalid"
    assert proof.diagnostic == "metadata_prefix_malformed"


def test_missing_file_is_an_explicit_typed_marker(tmp_path):
    proof = prove_sidecar(tmp_path, "missing", "default")

    assert proof.status == "missing"
    assert proof.diagnostic == "sidecar_missing"
    assert proof.sidecar_generation is None
    assert proof.stat_signature is None
    assert proof.route_metadata == {}


def test_unsafe_id_cannot_name_a_sidecar(tmp_path):
    proof = prove_sidecar(tmp_path, "../session", "default")

    assert proof.status == "invalid"
    assert proof.diagnostic == "unsafe_session_id"


@pytest.mark.parametrize(
    ("overrides", "diagnostic"),
    [
        ({"session_id": "other"}, "session_id_mismatch"),
        ({"sidecar_generation": "4"}, "invalid_sidecar_generation"),
        ({"sidecar_generation": True}, "invalid_sidecar_generation"),
        ({"sidecar_generation": -1}, "invalid_sidecar_generation"),
        ({"truncation_watermark": float("nan")}, "invalid_truncation_watermark"),
        ({"truncation_watermark": math.inf}, "invalid_truncation_watermark"),
    ],
)
def test_malformed_required_metadata_fails_closed(tmp_path, overrides, diagnostic):
    _write_sidecar(tmp_path, file_session_id="session", **overrides)

    proof = prove_sidecar(tmp_path, "session", "default")

    assert proof.status == "invalid"
    assert proof.diagnostic == diagnostic


def test_profile_identity_uses_existing_root_alias_semantics_without_generic_normalization(tmp_path):
    _write_sidecar(tmp_path, profile="work")

    proof = prove_sidecar(tmp_path, "session", "default")

    assert proof.status == "invalid"
    assert proof.diagnostic == "profile_mismatch"


def test_profile_identity_keeps_the_established_unscoped_root_convention(tmp_path):
    _write_sidecar(tmp_path, profile=None)

    proof = prove_sidecar(tmp_path, "session", "default")

    assert proof.status == "present"
    assert proof.route_metadata["profile"] == "default"


def test_symlink_and_fifo_are_rejected_without_following_or_blocking(tmp_path):
    target = _write_sidecar(tmp_path, session_id="target")
    (tmp_path / "session.json").symlink_to(target)

    symlink_proof = prove_sidecar(tmp_path, "session", "default")
    assert symlink_proof.status == "unreadable"
    assert symlink_proof.diagnostic == "unsafe_sidecar_type"

    fifo = tmp_path / "fifo.json"
    try:
        os.mkfifo(fifo)
    except (AttributeError, NotImplementedError):
        pytest.skip("named pipes are unavailable on this platform")

    fifo_proof = prove_sidecar(tmp_path, "fifo", "default")
    assert fifo_proof.status == "unreadable"
    assert fifo_proof.diagnostic == "unsafe_sidecar_type"


def test_atomic_replace_during_the_bounded_read_is_rejected(tmp_path, monkeypatch):
    _write_sidecar(tmp_path)
    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps(
            {
                "session_id": "session",
                "profile": "default",
                "sidecar_generation": 5,
                "truncation_watermark": None,
                "messages": [],
            }
        ),
        encoding="utf-8",
    )
    import api.bounded_sidecar_proof as proof_module

    original = proof_module._read_metadata_prefix_from_fd

    def replace_after_read(fd):
        value = original(fd)
        os.replace(replacement, tmp_path / "session.json")
        return value

    monkeypatch.setattr(proof_module, "_read_metadata_prefix_from_fd", replace_after_read)

    proof = prove_sidecar(tmp_path, "session", "default")

    assert proof.status == "invalid"
    assert proof.diagnostic == "sidecar_changed_during_read"


def test_lineage_preserves_order_in_an_immutable_vector_and_keeps_missing_markers(tmp_path):
    _write_sidecar(tmp_path, "root")
    _write_sidecar(tmp_path, "tip", sidecar_generation=5)

    lineage = prove_sidecar_lineage(tmp_path, ("tip", "missing", "root"), "default")

    assert lineage.member_ids == ("tip", "missing", "root")
    assert tuple(member.session_id for member in lineage.members) == lineage.member_ids
    assert tuple(member.status for member in lineage.members) == ("present", "missing", "present")
    assert isinstance(lineage.members, tuple)
    with pytest.raises(TypeError):
        lineage.members[0] = lineage.members[1]  # type: ignore[index]


def test_lineage_caps_members_and_rejects_duplicates_and_unsafe_ids(tmp_path):
    with pytest.raises(SidecarProofInputError, match="duplicate"):
        prove_sidecar_lineage(tmp_path, ("one", "one"), "default")
    with pytest.raises(SidecarProofInputError, match="256"):
        prove_sidecar_lineage(tmp_path, tuple(f"s{i}" for i in range(257)), "default")
    with pytest.raises(SidecarProofInputError, match="safe"):
        prove_sidecar_lineage(tmp_path, ("../escape",), "default")
    with pytest.raises(SidecarProofInputError, match="safe"):
        prove_sidecar_lineage(tmp_path, ({"not": "an id"},), "default")


def test_lineage_rejects_unbounded_iterables_before_consuming_them(tmp_path):
    consumed = False

    def unbounded_members():
        nonlocal consumed
        consumed = True
        raise AssertionError("the iterable must be rejected without consumption")
        yield "member"  # pragma: no cover - keeps this a generator

    with pytest.raises(SidecarProofInputError):
        prove_sidecar_lineage(tmp_path, unbounded_members(), "default")

    assert consumed is False
