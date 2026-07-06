"""In-memory reference implementation of the ticket MCP contract.

Ships with the contract package for two jobs: the live example an owner's
wrapper engineer reads next to CONTRACT.md, and the second server the
platform's substitutability tests run against (proving "swap the server,
change nothing on the agent side"). Single-tenant by design — any bearer
token is accepted, mirroring an owner deployment where the wrapper guards
its own perimeter.

Run standalone:

    python -m api.services.tickets.reference_server --port 9100

then point a `ticket_mcp_server` config (or the conformance CLI) at
http://127.0.0.1:9100/mcp with any token.

No database, no Dograh imports beyond the contract module: this file is
readable as a spec.
"""

from datetime import UTC, datetime

from api.services.tickets import contract
from api.services.tickets.sanitize import (
    TicketValidationError,
    require_caller_number,
    require_note_type,
    require_text,
    require_ticket_id,
    validate_summary_content,
)


class InMemoryTicketStore:
    def __init__(self):
        self._by_run: dict[int, dict] = {}
        self._by_ticket_id: dict[str, dict] = {}

    def _view(self, ticket: dict) -> dict:
        return {**ticket, "contract_version": contract.CONTRACT_VERSION}

    def create_ticket(
        self,
        ticket_id: str,
        workflow_run_id: int,
        caller_number: str = "",
        room_name: str = "",
        transfer_reason: str = "",
    ) -> dict:
        try:
            ticket_id = require_ticket_id(ticket_id)
            caller_number = require_caller_number(caller_number)
            room_name = require_text(room_name, "room_name", contract.ROOM_NAME_MAX_LEN)
            transfer_reason = require_text(
                transfer_reason, "transfer_reason", contract.TRANSFER_REASON_MAX_LEN
            )
            if not isinstance(workflow_run_id, int) or workflow_run_id <= 0:
                raise TicketValidationError(
                    "workflow_run_id must be a positive integer"
                )
        except TicketValidationError as e:
            return contract.error_envelope(
                contract.ERROR_VALIDATION_FAILED, str(e), False
            )

        existing = self._by_run.get(workflow_run_id)
        if existing is not None:
            # Idempotent get-or-create: the first ticket wins, including its id.
            return {**self._view(existing), "created": False}

        ticket = {
            "ticket_id": ticket_id,
            "workflow_run_id": workflow_run_id,
            "caller_number": caller_number,
            "room_name": room_name,
            "transfer_reason": transfer_reason,
            "summary": None,
            "notes": [],
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._by_run[workflow_run_id] = ticket
        self._by_ticket_id[ticket_id] = ticket
        return {**self._view(ticket), "created": True}

    def append_ticket_note(self, ticket_id: str, note_type: str, content) -> dict:
        try:
            ticket_id = require_ticket_id(ticket_id)
            note_type = require_note_type(note_type)
            if isinstance(content, str):
                require_text(content, "content", contract.NOTE_CONTENT_MAX_LEN)
            elif isinstance(content, dict):
                # Same strict posture as the built-in server: unknown summary
                # fields are rejected, never stored (the contract's "never
                # store fields outside the contract").
                validate_summary_content(content)
            else:
                raise TicketValidationError("content must be a string or object")
        except TicketValidationError as e:
            return contract.error_envelope(
                contract.ERROR_VALIDATION_FAILED, str(e), False
            )

        ticket = self._by_ticket_id.get(ticket_id)
        if ticket is None:
            return contract.error_envelope(
                contract.ERROR_NOT_FOUND, f"ticket {ticket_id} not found", False
            )
        ticket["notes"] = [
            *ticket["notes"],
            {
                "note_type": note_type,
                "content": content,
                "created_at": datetime.now(UTC).isoformat(),
            },
        ]
        if note_type == "summary" and isinstance(content, dict):
            ticket["summary"] = content
        return self._view(ticket)

    def get_ticket(self, ticket_id: str) -> dict:
        try:
            ticket_id = require_ticket_id(ticket_id)
        except TicketValidationError as e:
            return contract.error_envelope(
                contract.ERROR_VALIDATION_FAILED, str(e), False
            )
        ticket = self._by_ticket_id.get(ticket_id)
        if ticket is None:
            return contract.error_envelope(
                contract.ERROR_NOT_FOUND, f"ticket {ticket_id} not found", False
            )
        return self._view(ticket)

    def find_tickets_by_caller(self, caller_number: str, limit: int = 5) -> dict:
        try:
            caller_number = require_caller_number(caller_number)
            if not isinstance(limit, int) or limit < 1:
                raise TicketValidationError("limit must be a positive integer")
        except TicketValidationError as e:
            return contract.error_envelope(
                contract.ERROR_VALIDATION_FAILED, str(e), False
            )
        limit = min(limit, contract.FIND_TICKETS_MAX_LIMIT)
        matches = [
            self._view(t)
            for t in self._by_ticket_id.values()
            if caller_number and t["caller_number"] == caller_number
        ]
        matches.sort(key=lambda t: t["created_at"], reverse=True)
        return {
            "tickets": matches[:limit],
            "contract_version": contract.CONTRACT_VERSION,
        }


def build_reference_mcp(store: InMemoryTicketStore | None = None):
    """A FastMCP server exposing the store as the four contract tools."""
    from fastmcp import FastMCP

    store = store or InMemoryTicketStore()
    mcp = FastMCP("ticket-mcp-reference")

    @mcp.tool
    def create_ticket(
        ticket_id: str,
        workflow_run_id: int,
        caller_number: str = "",
        room_name: str = "",
        transfer_reason: str = "",
    ) -> dict:
        """Create a handoff ticket (idempotent get-or-create on workflow_run_id)."""
        return store.create_ticket(
            ticket_id, workflow_run_id, caller_number, room_name, transfer_reason
        )

    @mcp.tool
    def append_ticket_note(ticket_id: str, note_type: str, content) -> dict:
        """Append a note; never mutates existing fields."""
        return store.append_ticket_note(ticket_id, note_type, content)

    @mcp.tool
    def get_ticket(ticket_id: str) -> dict:
        """Fetch a ticket by its correlation key."""
        return store.get_ticket(ticket_id)

    @mcp.tool
    def find_tickets_by_caller(caller_number: str, limit: int = 5) -> dict:
        """Recent tickets for an E.164 caller number, most recent first."""
        return store.find_tickets_by_caller(caller_number, limit)

    return mcp


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Ticket MCP reference server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()

    mcp = build_reference_mcp()
    mcp.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
