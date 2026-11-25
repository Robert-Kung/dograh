"""Canonical-shape checks for org-scoped object storage keys.

An org-prefix check (``{prefix}/{org_id}/...``) only holds if the key cannot
step out of that prefix: ``..``/``.`` segments, empty segments (``//``,
leading or trailing ``/``) and backslashes are rejected, since a backend that
normalizes paths would resolve them into another org's namespace.
"""


def key_segments(storage_key: str) -> list[str] | None:
    """The key's ``/`` segments, or None if any segment is non-canonical."""
    parts = storage_key.split("/")
    if any(seg in ("", ".", "..") or "\\" in seg for seg in parts):
        return None
    return parts


def is_org_scoped_key(storage_key: str, prefix: str, organization_id: int) -> bool:
    """True for a canonical ``{prefix}/{organization_id}/<name>...`` key."""
    parts = key_segments(storage_key)
    return (
        parts is not None
        and len(parts) >= 3
        and parts[0] == prefix
        and parts[1] == str(organization_id)
    )
