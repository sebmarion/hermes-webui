"""Fail-closed, descriptor-verified sidecar metadata proofs.

This module deliberately has no session-directory global and no route coupling.
It proves the compact metadata for an explicit sidecar directory only; callers
must treat every status other than ``present`` as ineligible for a fast path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import stat as stat_module
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from api.models import _find_top_level_json_key, is_safe_session_id
from api.profiles import _profiles_match


MAX_METADATA_PREFIX_BYTES = 64 * 1024
MAX_LINEAGE_MEMBERS = 256
_READ_CHUNK_BYTES = 4096
_MAX_ROUTE_TEXT_BYTES = 4096
_ROUTE_TEXT_FIELDS = (
    "title",
    "workspace",
    "model",
    "model_provider",
    "project_id",
    "parent_session_id",
    "session_source",
    "source_tag",
    "source_label",
)


class SidecarProofInputError(ValueError):
    """The caller supplied an unsafe or unbounded proof request."""


class _PrefixReadError(ValueError):
    diagnostic: str


class _PrefixTooLarge(_PrefixReadError):
    diagnostic = "metadata_prefix_too_large"


class _PrefixMalformed(_PrefixReadError):
    diagnostic = "metadata_prefix_malformed"


@dataclass(frozen=True)
class SidecarStatSignature:
    """Exact stable identity of the opened file, without exposing its path."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class SidecarMetadataProof:
    """A content-free result for one sidecar.

    ``diagnostic`` is deliberately a closed vocabulary: it must never contain
    filenames, hashes, exception text, or parsed sidecar values.
    """

    session_id: str
    status: str
    diagnostic: str
    stat_signature: SidecarStatSignature | None = None
    sidecar_generation: int | None = None
    truncation_watermark: int | float | None = None
    route_metadata: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class SidecarLineageProof:
    """An ordered immutable vector; missing sidecars stay explicit members."""

    member_ids: tuple[str, ...]
    members: tuple[SidecarMetadataProof, ...]

    @property
    def complete(self) -> bool:
        return all(member.status == "present" for member in self.members)


def prove_sidecar(
    session_dir: str | os.PathLike[str], session_id: str, profile: str | None
) -> SidecarMetadataProof:
    """Read at most 64 KiB of trusted compact metadata for one sidecar.

    The existing models metadata-prefix parser reopens a path, which cannot
    preserve an inode proof across a replacement race.  This implementation
    therefore reuses its top-level-key scanner while reading the already-open,
    non-symlink descriptor.  ``lstat``/``fstat``/final ``lstat`` must all agree
    before any parsed metadata is returned.
    """
    if not is_safe_session_id(session_id):
        return _result(session_id, "invalid", "unsafe_session_id")
    try:
        directory = Path(session_dir)
    except (TypeError, ValueError):
        return _result(session_id, "invalid", "invalid_session_dir")
    path = directory / f"{session_id}.json"

    try:
        before = os.lstat(path)
    except FileNotFoundError:
        # A second lstat closes the no-read create race without converting a
        # stable absence into an exception or a false positive proof.
        try:
            os.lstat(path)
        except FileNotFoundError:
            return _result(session_id, "missing", "sidecar_missing")
        except OSError:
            return _result(session_id, "unreadable", "sidecar_unreadable")
        return _result(session_id, "invalid", "sidecar_changed_during_read")
    except OSError:
        return _result(session_id, "unreadable", "sidecar_unreadable")

    if not stat_module.S_ISREG(before.st_mode):
        return _result(session_id, "unreadable", "unsafe_sidecar_type")
    before_signature = _signature(before)

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return _result(session_id, "invalid", "sidecar_changed_during_read")
    except OSError:
        return _result(session_id, "unreadable", "sidecar_unreadable")

    prefix: str | None = None
    prefix_error: str | None = None
    descriptor_signature: SidecarStatSignature | None = None
    try:
        opened = os.fstat(fd)
        descriptor_signature = _signature(opened)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or descriptor_signature != before_signature
        ):
            return _result(
                session_id,
                "invalid",
                "sidecar_changed_during_read",
                stat_signature=descriptor_signature,
            )
        try:
            prefix = _read_metadata_prefix_from_fd(fd)
        except _PrefixReadError as exc:
            prefix_error = exc.diagnostic
        except (OSError, UnicodeDecodeError):
            return _result(
                session_id,
                "unreadable",
                "sidecar_unreadable",
                stat_signature=descriptor_signature,
            )

        after_fd_signature = _signature(os.fstat(fd))
        try:
            after_path_signature = _signature(os.lstat(path))
        except OSError:
            after_path_signature = None
        if (
            descriptor_signature != before_signature
            or after_fd_signature != before_signature
            or after_path_signature != before_signature
        ):
            return _result(
                session_id,
                "invalid",
                "sidecar_changed_during_read",
                stat_signature=after_fd_signature,
            )
    finally:
        os.close(fd)

    if prefix_error is not None:
        return _result(
            session_id, "invalid", prefix_error, stat_signature=before_signature
        )
    if prefix is None:  # Defensive: the bounded reader either returns or raises.
        return _result(
            session_id,
            "invalid",
            "metadata_prefix_malformed",
            stat_signature=before_signature,
        )
    try:
        parsed = json.loads(prefix)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _result(
            session_id,
            "invalid",
            "metadata_prefix_malformed",
            stat_signature=before_signature,
        )
    if type(parsed) is not dict:
        return _result(
            session_id,
            "invalid",
            "metadata_prefix_malformed",
            stat_signature=before_signature,
        )
    return _validate_metadata(
        session_id, profile, parsed, stat_signature=before_signature
    )


def prove_sidecar_lineage(
    session_dir: str | os.PathLike[str], member_ids: Sequence[str], profile: str | None
) -> SidecarLineageProof:
    """Prove one ordered lineage without discovery, sorting, or omitted holes."""
    if type(member_ids) not in (list, tuple):
        raise SidecarProofInputError("lineage members must be a bounded list or tuple")
    if not 1 <= len(member_ids) <= MAX_LINEAGE_MEMBERS:
        raise SidecarProofInputError(
            f"lineage must contain 1..{MAX_LINEAGE_MEMBERS} members"
        )
    members = tuple(member_ids)
    if not all(is_safe_session_id(member_id) for member_id in members):
        raise SidecarProofInputError("lineage member ids must all be safe")
    if len(set(members)) != len(members):
        raise SidecarProofInputError("lineage member ids must be unique; duplicate found")
    proofs = tuple(prove_sidecar(session_dir, member_id, profile) for member_id in members)
    return SidecarLineageProof(member_ids=members, members=proofs)


def _read_metadata_prefix_from_fd(fd: int) -> str:
    """Bounded descriptor reader equivalent to models' prefix parser.

    The scan stops before the first top-level ``messages`` or
    ``anchor_activity_scenes`` key, so full message/tool/scene bodies never
    enter the result even for legacy sidecar orderings.
    """
    chunks: list[bytes] = []
    total = 0
    while total < MAX_METADATA_PREFIX_BYTES:
        chunk = os.read(fd, min(_READ_CHUNK_BYTES, MAX_METADATA_PREFIX_BYTES - total))
        if not chunk:
            raise _PrefixMalformed()
        chunks.append(chunk)
        total += len(chunk)
        try:
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            # A complete chunk can end in a multi-byte codepoint. Try the next
            # chunk while the cap remains; invalid completed UTF-8 is malformed.
            if total < MAX_METADATA_PREFIX_BYTES:
                continue
            raise _PrefixMalformed() from exc
        stop = _find_top_level_json_key(text, "messages")
        scenes = _find_top_level_json_key(text, "anchor_activity_scenes")
        if scenes is not None and (stop is None or scenes < stop):
            stop = scenes
        if stop is None:
            continue
        prefix = text[:stop].rstrip()
        if prefix.endswith(","):
            prefix = prefix[:-1].rstrip()
        return f"{prefix}\n}}"
    raise _PrefixTooLarge()


def _validate_metadata(
    session_id: str,
    expected_profile: str | None,
    parsed: Mapping[str, Any],
    *,
    stat_signature: SidecarStatSignature,
) -> SidecarMetadataProof:
    if type(parsed.get("session_id")) is not str or parsed["session_id"] != session_id:
        return _result(session_id, "invalid", "session_id_mismatch", stat_signature=stat_signature)
    generation = parsed.get("sidecar_generation")
    if type(generation) is not int or generation < 0:
        return _result(
            session_id, "invalid", "invalid_sidecar_generation", stat_signature=stat_signature
        )
    watermark = parsed.get("truncation_watermark")
    if watermark is not None and not _finite_number(watermark):
        return _result(
            session_id,
            "invalid",
            "invalid_truncation_watermark",
            stat_signature=stat_signature,
        )
    sidecar_profile = parsed.get("profile")
    if sidecar_profile is not None and type(sidecar_profile) is not str:
        return _result(session_id, "invalid", "profile_mismatch", stat_signature=stat_signature)
    try:
        profiles_match = _profiles_match(sidecar_profile, expected_profile)
    except Exception:
        profiles_match = False
    if not profiles_match:
        return _result(session_id, "invalid", "profile_mismatch", stat_signature=stat_signature)
    return _result(
        session_id,
        "present",
        "ok",
        stat_signature=stat_signature,
        sidecar_generation=generation,
        truncation_watermark=watermark,
        route_metadata=_compact_route_metadata(parsed, session_id, sidecar_profile),
    )


def _compact_route_metadata(
    parsed: Mapping[str, Any], session_id: str, profile: str | None
) -> Mapping[str, Any]:
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "profile": profile if profile is not None else "default",
    }
    for field in _ROUTE_TEXT_FIELDS:
        metadata[field] = _bounded_text(parsed.get(field))
    for field in ("created_at", "updated_at"):
        value = parsed.get(field)
        metadata[field] = value if _finite_number(value) else None
    for field in ("pinned", "archived", "is_cli_session", "read_only"):
        metadata[field] = value if type(value := parsed.get(field)) is bool else False
    count = parsed.get("message_count")
    metadata["message_count"] = count if type(count) is int and count >= 0 else None
    return MappingProxyType(metadata)


def _bounded_text(value: Any) -> str | None:
    if type(value) is not str or len(value.encode("utf-8")) > _MAX_ROUTE_TEXT_BYTES:
        return None
    return value


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _signature(st: os.stat_result) -> SidecarStatSignature:
    return SidecarStatSignature(
        device=int(st.st_dev),
        inode=int(st.st_ino),
        mode=int(st.st_mode),
        size=int(st.st_size),
        mtime_ns=int(st.st_mtime_ns),
        ctime_ns=int(st.st_ctime_ns),
    )


def _result(
    session_id: str,
    status: str,
    diagnostic: str,
    *,
    stat_signature: SidecarStatSignature | None = None,
    sidecar_generation: int | None = None,
    truncation_watermark: int | float | None = None,
    route_metadata: Mapping[str, Any] | None = None,
) -> SidecarMetadataProof:
    return SidecarMetadataProof(
        session_id=session_id,
        status=status,
        diagnostic=diagnostic,
        stat_signature=stat_signature,
        sidecar_generation=sidecar_generation,
        truncation_watermark=truncation_watermark,
        route_metadata=route_metadata if route_metadata is not None else MappingProxyType({}),
    )
