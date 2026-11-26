"""Canonical-shape checks for org-scoped object storage keys.

An org-prefix check (``{prefix}/{org_id}/...``) only holds if the key cannot
step out of that prefix: ``..``/``.`` segments, empty segments (``//``,
leading or trailing ``/``) and backslashes are rejected, since a backend that
normalizes paths would resolve them into another org's namespace.
"""

import re
import unicodedata
from urllib.parse import unquote

_ORG_SEGMENT = re.compile(r"[1-9][0-9]*")


def _unsafe(seg: str) -> bool:
    if any(ord(c) < 32 or ord(c) == 127 for c in seg):
        return True
    # A backend (or proxy) that URL-decodes or NFKC-normalizes the key must not
    # turn a segment into a traversal: "%2e%2e", fullwidth "．．", "%2f".
    for form in {
        seg,
        unquote(seg),
        unicodedata.normalize("NFKC", seg),
        unicodedata.normalize("NFKC", unquote(seg)),
    }:
        if form in ("", ".", "..") or "/" in form or "\\" in form:
            return True
    return False


def _breaks_segment(c: str) -> bool:
    if ord(c) < 32 or ord(c) == 127:
        return True
    form = unicodedata.normalize("NFKC", c)
    return "/" in form or "\\" in form


def key_segment_name(name: str) -> str:
    """``name`` as one canonical key segment, kept readable (round 2 R9).

    Only what :func:`key_segments` would refuse is changed: separators (also
    as NFKC forms) and control characters become ``_``, a ``%`` is replaced
    only where decoding would make a separator or a dot segment, and an
    empty, ``.`` or ``..`` name gets a ``_`` prefix. Other names, Chinese
    ones included, are kept as they are.
    """
    cleaned = "".join("_" if _breaks_segment(c) else c for c in name)
    if _unsafe(cleaned):
        cleaned = cleaned.replace("%", "_")
    if _unsafe(cleaned):
        cleaned = f"_{cleaned}"
    return cleaned


def key_segments(storage_key: str) -> list[str] | None:
    """The key's ``/`` segments, or None if any segment is non-canonical.

    Names stay free-form (KB uploads keep the original, e.g. Chinese, file
    name); only what could resolve into another prefix is refused.
    """
    parts = storage_key.split("/")
    if any(_unsafe(seg) for seg in parts):
        return None
    return parts


def org_segment(segment: str) -> int | None:
    """ASCII positive integer only (``isdigit`` also accepts "１")."""
    return int(segment) if _ORG_SEGMENT.fullmatch(segment) else None


def is_org_scoped_key(storage_key: str, prefix: str, organization_id: int) -> bool:
    """True for a canonical ``{prefix}/{organization_id}/<name>...`` key."""
    parts = key_segments(storage_key)
    return (
        parts is not None
        and len(parts) >= 3
        and parts[0] == prefix
        and parts[1] == str(organization_id)
    )


_NON_ASCII_NAME_CHARS = re.compile(r"[^a-zA-Z0-9._-]")


def ascii_name(name: str) -> str:
    """``name`` with every character outside ``[a-zA-Z0-9._-]`` made ``_``."""
    return _NON_ASCII_NAME_CHARS.sub("_", name)


def safe_filename(name: str) -> str:
    """:func:`ascii_name`, never empty or a dot segment: one canonical key
    segment (workflow recording uploads)."""
    cleaned = ascii_name(name)
    return "_" if cleaned in ("", ".", "..") else cleaned


def is_own_recording_upload_key(
    storage_key: str, organization_id: int, recording_id: str
) -> bool:
    """True only for the exact shape the recording upload-url issues:
    recordings/{org_id}/{recording_id}/{filename}.

    A prefix check is not enough: call recordings live at
    recordings/{run_id}/user.wav, and run ids overlap org ids, so
    "recordings/{own org}/user.wav" would name another org's call audio.
    """
    parts = key_segments(storage_key)
    return (
        parts is not None
        and len(parts) == 4
        and parts[0] == "recordings"
        and parts[1] == str(organization_id)
        and parts[2] == recording_id
    )
