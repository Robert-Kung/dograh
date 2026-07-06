"""Ticket MCP tools (S-L4-SCREENPOP) — built-in implementation of the
ticket contract (contract_version, field constraints, and the error
envelope all come from `api.services.tickets.contract`).

This is the default/reference server; owner deployments may replace it
with any MCP server implementing the REQUIRED tools (C5). Failures are
returned as the contract's structured error envelope, not raised — every
implementation must produce the same shape.

Org isolation: the target organization is always the credential's
organization (org_id is not part of the contract schema); a foreign-org
ticket is indistinguishable from a missing one.
"""

from loguru import logger

from api.db import db_client
from api.mcp_server.auth import authenticate_mcp_request
from api.mcp_server.tracing import traced_tool
from api.services.tickets import contract
from api.services.tickets.sanitize import (
    TicketValidationError,
    require_caller_number,
    require_note_type,
    require_text,
    require_ticket_id,
    validate_summary_content,
)


def _ticket_view(ticket) -> dict:
    return {
        "ticket_id": ticket.ticket_id,
        "workflow_run_id": ticket.workflow_run_id,
        "caller_number": ticket.caller_number,
        "room_name": ticket.room_name,
        "transfer_reason": ticket.transfer_reason,
        "summary": ticket.summary,
        "notes": ticket.notes or [],
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "contract_version": contract.CONTRACT_VERSION,
    }


@traced_tool
async def create_ticket(
    ticket_id: str,
    workflow_run_id: int,
    caller_number: str = "",
    room_name: str = "",
    transfer_reason: str = "",
) -> dict:
    """Create a handoff ticket (idempotent get-or-create).

    The idempotency key is `workflow_run_id`: calling again — a timeout
    retry, a double trigger — returns the existing ticket instead of a
    second one. `ticket_id` is the platform-generated correlation key
    carried in REFER headers; `caller_number` is E.164 or empty
    (anonymous). Failures return `{"error": {code, message, retryable}}`.
    """
    user = await authenticate_mcp_request()
    try:
        ticket_id = require_ticket_id(ticket_id)
        caller_number = require_caller_number(caller_number)
        room_name = require_text(room_name, "room_name", contract.ROOM_NAME_MAX_LEN)
        transfer_reason = require_text(
            transfer_reason, "transfer_reason", contract.TRANSFER_REASON_MAX_LEN
        )
        if not isinstance(workflow_run_id, int) or workflow_run_id <= 0:
            raise TicketValidationError("workflow_run_id must be a positive integer")
    except TicketValidationError as e:
        return contract.error_envelope(contract.ERROR_VALIDATION_FAILED, str(e), False)

    try:
        ticket, created = await db_client.create_ticket(
            organization_id=user.selected_organization_id,
            ticket_id=ticket_id,
            workflow_run_id=workflow_run_id,
            caller_number=caller_number,
            room_name=room_name,
            transfer_reason=transfer_reason,
        )
    except Exception as e:
        logger.error(f"create_ticket backend failure: {e}")
        return contract.error_envelope(
            contract.ERROR_UNAVAILABLE, "ticket store unavailable", True
        )
    return {**_ticket_view(ticket), "created": created}


@traced_tool
async def append_ticket_note(ticket_id: str, note_type: str, content) -> dict:
    """Append a note to a ticket (never mutates existing fields).

    `note_type` is one of "summary" (structured handoff summary dict),
    "transfer_failed", or "generic". Content is stored as data, never
    interpreted. Failures return `{"error": {code, message, retryable}}`.
    """
    user = await authenticate_mcp_request()
    try:
        ticket_id = require_ticket_id(ticket_id)
        note_type = require_note_type(note_type)
        if isinstance(content, str):
            require_text(content, "content", contract.NOTE_CONTENT_MAX_LEN)
        elif isinstance(content, dict):
            validate_summary_content(content)
        else:
            raise TicketValidationError("content must be a string or object")
    except TicketValidationError as e:
        return contract.error_envelope(contract.ERROR_VALIDATION_FAILED, str(e), False)

    try:
        ticket = await db_client.append_ticket_note(
            organization_id=user.selected_organization_id,
            ticket_id=ticket_id,
            note_type=note_type,
            content=content,
        )
    except Exception as e:
        logger.error(f"append_ticket_note backend failure: {e}")
        return contract.error_envelope(
            contract.ERROR_UNAVAILABLE, "ticket store unavailable", True
        )
    if ticket is None:
        return contract.error_envelope(
            contract.ERROR_NOT_FOUND, f"ticket {ticket_id} not found", False
        )
    return _ticket_view(ticket)


@traced_tool
async def get_ticket(ticket_id: str) -> dict:
    """Fetch a ticket by its correlation key (the id carried in REFER headers).

    Failures return `{"error": {code, message, retryable}}`.
    """
    user = await authenticate_mcp_request()
    try:
        ticket_id = require_ticket_id(ticket_id)
    except TicketValidationError as e:
        return contract.error_envelope(contract.ERROR_VALIDATION_FAILED, str(e), False)

    try:
        ticket = await db_client.get_ticket(
            organization_id=user.selected_organization_id, ticket_id=ticket_id
        )
    except Exception as e:
        logger.error(f"get_ticket backend failure: {e}")
        return contract.error_envelope(
            contract.ERROR_UNAVAILABLE, "ticket store unavailable", True
        )
    if ticket is None:
        return contract.error_envelope(
            contract.ERROR_NOT_FOUND, f"ticket {ticket_id} not found", False
        )
    return _ticket_view(ticket)


@traced_tool
async def find_tickets_by_caller(caller_number: str, limit: int = 5) -> dict:
    """Find recent tickets for an E.164 caller number (screen-pop fallback).

    Most-recent-first, capped at 20. An empty result is a legal state
    (anonymous callers never match by number). Failures return
    `{"error": {code, message, retryable}}`.
    """
    user = await authenticate_mcp_request()
    try:
        caller_number = require_caller_number(caller_number)
        if not isinstance(limit, int) or limit < 1:
            raise TicketValidationError("limit must be a positive integer")
    except TicketValidationError as e:
        return contract.error_envelope(contract.ERROR_VALIDATION_FAILED, str(e), False)

    try:
        tickets = await db_client.find_tickets_by_caller(
            organization_id=user.selected_organization_id,
            caller_number=caller_number,
            limit=min(limit, contract.FIND_TICKETS_MAX_LIMIT),
        )
    except Exception as e:
        logger.error(f"find_tickets_by_caller backend failure: {e}")
        return contract.error_envelope(
            contract.ERROR_UNAVAILABLE, "ticket store unavailable", True
        )
    return {
        "tickets": [_ticket_view(t) for t in tickets],
        "contract_version": contract.CONTRACT_VERSION,
    }
