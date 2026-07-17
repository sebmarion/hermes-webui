from dataclasses import replace

import pytest


def _claims(**overrides):
    from api.session_message_paging import MessageCursorBoundary, MessageCursorClaims

    values = {
        "version": 1,
        "profile": "default",
        "canonical_id": "tip",
        "lineage_fingerprint": "sha256:" + ("a" * 64),
        "source_mode": "state_db",
        "database_identity_digest": "sha256:" + ("b" * 64),
        "global_generation_hint": 7,
        "receipt_generation": None,
        "boundaries": (
            MessageCursorBoundary("root", 12.5, 9),
            MessageCursorBoundary("tip", None, 4),
        ),
    }
    values.update(overrides)
    return MessageCursorClaims(**values)


def _expected(claims=None, **overrides):
    from api.session_message_paging import MessageCursorExpected

    claims = claims or _claims()
    values = {
        "profile": claims.profile,
        "canonical_id": claims.canonical_id,
        "lineage_fingerprint": claims.lineage_fingerprint,
        "source_mode": claims.source_mode,
        "database_identity_digest": claims.database_identity_digest,
        "global_generation_hint": claims.global_generation_hint,
        "receipt_generation": claims.receipt_generation,
        "member_ids": tuple(
            boundary.member_id for boundary in claims.boundaries
        ),
    }
    values.update(overrides)
    return MessageCursorExpected(**values)


def test_cursor_round_trip_is_deterministic_and_canonical():
    from api.session_message_paging import (
        decode_message_cursor,
        encode_message_cursor,
    )

    claims = _claims()
    key = b"k" * 32
    first = encode_message_cursor(claims, signing_key=key)
    second = encode_message_cursor(claims, signing_key=key)

    assert first == second
    assert first.count(".") == 1
    assert decode_message_cursor(
        first,
        signing_key=key,
        expected=_expected(claims),
    ) == claims
    assert "/private/" not in first


def test_cursor_round_trip_preserves_large_integer_timestamp_exactly():
    from api.session_message_paging import (
        MessageCursorBoundary,
        decode_message_cursor,
        encode_message_cursor,
    )

    claims = _claims(
        boundaries=(
            MessageCursorBoundary("tip", 9007199254740993, 1),
        ),
    )
    token = encode_message_cursor(claims, signing_key=b"k" * 32)

    decoded = decode_message_cursor(
        token,
        signing_key=b"k" * 32,
        expected=_expected(claims),
    )

    assert decoded.boundaries[0].timestamp == 9007199254740993
    assert isinstance(decoded.boundaries[0].timestamp, int)


def test_cursor_compacts_maximum_lineage_below_wire_limit():
    from api.session_message_paging import (
        MAX_MESSAGE_CURSOR_TOKEN_BYTES,
        MessageCursorBoundary,
        decode_message_cursor,
        encode_message_cursor,
    )

    members = tuple(
        f"{index:03d}-12345678-1234-1234-1234-123456789abc"
        for index in range(256)
    )
    claims = _claims(
        boundaries=tuple(
            MessageCursorBoundary(
                member,
                9_223_372_036_854_775_807 - index,
                9_223_372_036_854_775_807 - index,
                bool(index % 2),
            )
            for index, member in enumerate(members)
        ),
    )

    token = encode_message_cursor(
        claims,
        signing_key=b"k" * 32,
        member_ids=members,
    )

    assert len(token.encode("ascii")) < MAX_MESSAGE_CURSOR_TOKEN_BYTES
    assert decode_message_cursor(
        token,
        signing_key=b"k" * 32,
        expected=_expected(claims),
    ) == claims


def test_cursor_tampering_and_key_rotation_fail_closed():
    from api.session_message_paging import (
        MessageCursorError,
        decode_message_cursor,
        encode_message_cursor,
    )

    claims = _claims()
    token = encode_message_cursor(claims, signing_key=b"a" * 32)
    payload, signature = token.split(".")
    replacement = "A" if signature[-1] != "A" else "B"
    tampered = f"{payload}.{signature[:-1]}{replacement}"

    with pytest.raises(MessageCursorError, match="signature"):
        decode_message_cursor(
            tampered,
            signing_key=b"a" * 32,
            expected=_expected(claims),
        )
    with pytest.raises(MessageCursorError, match="signature"):
        decode_message_cursor(
            token,
            signing_key=b"b" * 32,
            expected=_expected(claims),
        )


def test_oversized_cursor_is_rejected_before_base64_decode(monkeypatch):
    import api.session_message_paging as paging

    def forbidden(*_args, **_kwargs):
        raise AssertionError("oversized token reached base64 decoding")

    monkeypatch.setattr(paging.base64, "urlsafe_b64decode", forbidden)
    with pytest.raises(paging.MessageCursorError, match="too large"):
        paging.decode_message_cursor(
            "x" * (paging.MAX_MESSAGE_CURSOR_TOKEN_BYTES + 1),
            signing_key=b"k" * 32,
            expected=_expected(),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile", "other"),
        ("canonical_id", "other"),
        ("lineage_fingerprint", "sha256:" + ("c" * 64)),
        ("source_mode", "sidecar"),
        ("database_identity_digest", "sha256:" + ("d" * 64)),
        ("receipt_generation", 3),
    ],
)
def test_cursor_is_bound_to_expected_request_state(field, value):
    from api.session_message_paging import (
        MessageCursorError,
        decode_message_cursor,
        encode_message_cursor,
    )

    claims = _claims()
    token = encode_message_cursor(claims, signing_key=b"k" * 32)
    expected = replace(_expected(claims), **{field: value})

    with pytest.raises(MessageCursorError, match=field):
        decode_message_cursor(
            token,
            signing_key=b"k" * 32,
            expected=expected,
        )


def test_global_generation_hint_is_not_a_hard_cursor_binding():
    from api.session_message_paging import (
        decode_message_cursor,
        encode_message_cursor,
    )

    claims = _claims(global_generation_hint=7)
    token = encode_message_cursor(claims, signing_key=b"k" * 32)

    assert decode_message_cursor(
        token,
        signing_key=b"k" * 32,
        expected=replace(_expected(claims), global_generation_hint=99),
    ) == claims


def test_wrong_version_and_malformed_boundaries_are_rejected():
    from api.session_message_paging import (
        MessageCursorBoundary,
        MessageCursorError,
        decode_message_cursor,
        encode_message_cursor,
    )

    wrong_version = _claims(version=2)
    token = encode_message_cursor(wrong_version, signing_key=b"k" * 32)
    with pytest.raises(MessageCursorError, match="version"):
        decode_message_cursor(
            token,
            signing_key=b"k" * 32,
            expected=_expected(wrong_version),
        )

    duplicate = _claims(
        boundaries=(
            MessageCursorBoundary("root", 1.0, 2),
            MessageCursorBoundary("root", 0.0, 1),
        )
    )
    with pytest.raises(MessageCursorError, match="boundar"):
        encode_message_cursor(duplicate, signing_key=b"k" * 32)
