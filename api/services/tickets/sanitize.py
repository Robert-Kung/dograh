"""Ticket field sanitization (C6/C8-TRUST).

Used on both sides of the wire: the platform handoff client sanitizes
*before sending* (the C6 guarantee never depends on the owner's wrapper),
and the built-in server re-validates as defense in depth. Caller-derived
speech content is data, never instructions — sanitization here is
whitelist + length + control characters; semantic prompt injection is
S-L8 residual risk.
"""

import re

from api.services.tickets import contract

# Strip C0/C1 control chars except \n and \t (summaries are multi-line prose).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % contract.TICKET_ID_MAX_LEN)
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


class TicketValidationError(ValueError):
    """Raised by validators; maps to the contract's VALIDATION_FAILED."""


def clean_text(value: str, max_len: int) -> str:
    """Strip control characters and truncate — the pre-send (client) posture."""
    return _CONTROL_CHARS_RE.sub("", value)[:max_len]


def require_text(value, field: str, max_len: int) -> str:
    """Server-side posture: reject rather than silently fix (defense in depth)."""
    if not isinstance(value, str):
        raise TicketValidationError(f"{field} must be a string")
    if _CONTROL_CHARS_RE.search(value):
        raise TicketValidationError(f"{field} contains control characters")
    if len(value) > max_len:
        raise TicketValidationError(f"{field} exceeds {max_len} characters")
    return value


def require_ticket_id(value) -> str:
    if not isinstance(value, str) or not _TICKET_ID_RE.match(value):
        raise TicketValidationError("ticket_id must match ^[A-Za-z0-9_-]{1,64}$")
    return value


def require_caller_number(value) -> str:
    """E.164 or empty string (anonymous caller is a legal shape)."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _E164_RE.match(value):
        raise TicketValidationError("caller_number must be E.164 (+<digits>) or empty")
    return value


def require_note_type(value) -> str:
    if value not in contract.NOTE_TYPES:
        raise TicketValidationError(
            f"note_type must be one of {list(contract.NOTE_TYPES)}"
        )
    return value


def validate_summary_content(content: dict) -> None:
    """Server-side posture for summary-shaped note content: reject, don't fix.

    Field whitelist, text/list caps, and — because it lands in front of a
    human agent as trust signal — `verified_identity` restricted to its
    closed value set. Shared by the built-in server and the reference
    server so wrapper engineers copy the strict posture (C6/C8-TRUST).
    """
    for key, value in content.items():
        if key not in contract.SUMMARY_FIELDS:
            raise TicketValidationError(f"unknown summary field: {key}")
        if key == "verified_identity":
            if value not in contract.VERIFIED_IDENTITY_VALUES:
                raise TicketValidationError(
                    "verified_identity must be one of "
                    f"{list(contract.VERIFIED_IDENTITY_VALUES)}"
                )
        elif isinstance(value, str):
            require_text(value, key, contract.SUMMARY_TEXT_MAX_LEN)
        elif isinstance(value, list):
            if len(value) > contract.SUMMARY_LIST_MAX_ITEMS:
                raise TicketValidationError(f"{key} has too many items")
            for item in value:
                require_text(str(item), key, contract.SUMMARY_TEXT_MAX_LEN)
        else:
            raise TicketValidationError(f"{key} must be a string or list")


def clean_summary(summary: dict) -> dict:
    """Coerce a summary onto the fixed contract schema (client pre-send).

    Whitelist fields, clamp text/list sizes, and force `verified_identity`
    into its closed value set — anything unexpected degrades to "unknown",
    never to a trusted-looking value.
    """
    out = {}
    for field in contract.SUMMARY_FIELDS:
        value = (summary or {}).get(field, "unknown")
        if field in ("steps_done", "pending"):
            if not isinstance(value, list):
                value = []
            out[field] = [
                clean_text(str(item), contract.SUMMARY_TEXT_MAX_LEN)
                for item in value[: contract.SUMMARY_LIST_MAX_ITEMS]
            ]
        elif field == "verified_identity":
            out[field] = (
                value if value in contract.VERIFIED_IDENTITY_VALUES else "unknown"
            )
        else:
            out[field] = clean_text(str(value), contract.SUMMARY_TEXT_MAX_LEN)
    return out
