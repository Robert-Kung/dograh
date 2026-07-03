"""Ticket store operations (S-L4-SCREENPOP built-in ticket MCP server).

Every method is organization-scoped: a ticket belonging to another org is
indistinguishable from a missing one (no existence leak).
"""

from datetime import UTC, datetime, timedelta
from typing import Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select

from api.db.base_client import BaseDBClient
from api.db.models import TicketModel


class TicketClient(BaseDBClient):
    async def create_ticket(
        self,
        organization_id: int,
        ticket_id: str,
        workflow_run_id: int,
        caller_number: str = "",
        room_name: str = "",
        transfer_reason: str = "",
    ) -> tuple[TicketModel, bool]:
        """Get-or-create keyed on (organization_id, workflow_run_id).

        Returns (ticket, created). A duplicate call — timeout retry, a
        voice/press-0 double trigger — returns the existing row, whose
        ticket_id wins over the one supplied.
        """
        async with self.async_session() as session:
            existing = await session.execute(
                select(TicketModel).where(
                    TicketModel.organization_id == organization_id,
                    TicketModel.workflow_run_id == workflow_run_id,
                )
            )
            ticket = existing.scalars().first()
            if ticket:
                return ticket, False

            ticket = TicketModel(
                organization_id=organization_id,
                ticket_id=ticket_id,
                workflow_run_id=workflow_run_id,
                caller_number=caller_number,
                room_name=room_name,
                transfer_reason=transfer_reason,
                notes=[],
            )
            session.add(ticket)
            try:
                await session.commit()
            except IntegrityError:
                # Lost a concurrent-create race; the winner's row is the ticket.
                await session.rollback()
                result = await session.execute(
                    select(TicketModel).where(
                        TicketModel.organization_id == organization_id,
                        TicketModel.workflow_run_id == workflow_run_id,
                    )
                )
                return result.scalars().one(), False
            await session.refresh(ticket)
            return ticket, True

    async def get_ticket(
        self, organization_id: int, ticket_id: str
    ) -> Optional[TicketModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(TicketModel).where(
                    TicketModel.organization_id == organization_id,
                    TicketModel.ticket_id == ticket_id,
                )
            )
            return result.scalars().first()

    async def find_tickets_by_caller(
        self, organization_id: int, caller_number: str, limit: int
    ) -> list[TicketModel]:
        """Most-recent-first; empty caller_number never matches (anonymous
        tickets are reachable only by ticket_id)."""
        if not caller_number:
            return []
        async with self.async_session() as session:
            result = await session.execute(
                select(TicketModel)
                .where(
                    TicketModel.organization_id == organization_id,
                    TicketModel.caller_number == caller_number,
                )
                .order_by(TicketModel.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def append_ticket_note(
        self,
        organization_id: int,
        ticket_id: str,
        note_type: str,
        content,
    ) -> Optional[TicketModel]:
        """Append a note (row-locked read-modify-write). Returns None when the
        ticket doesn't exist in this org. A "summary" note also fills the
        ticket's summary column so the built-in store stays queryable."""
        async with self.async_session() as session:
            result = await session.execute(
                select(TicketModel)
                .where(
                    TicketModel.organization_id == organization_id,
                    TicketModel.ticket_id == ticket_id,
                )
                .with_for_update()
            )
            ticket = result.scalars().first()
            if not ticket:
                return None

            note = {
                "note_type": note_type,
                "content": content,
                "created_at": datetime.now(UTC).isoformat(),
            }
            ticket.notes = [*(ticket.notes or []), note]
            if note_type == "summary" and isinstance(content, dict):
                ticket.summary = content
            await session.commit()
            await session.refresh(ticket)
            return ticket

    async def get_ticket_organization_ids(self) -> list[int]:
        """Org ids that still hold non-anonymized tickets (retention sweep input)."""
        async with self.async_session() as session:
            result = await session.execute(
                select(TicketModel.organization_id)
                .where(TicketModel.anonymized_at.is_(None))
                .distinct()
            )
            return [row[0] for row in result.all()]

    async def anonymize_expired_tickets(
        self, organization_id: int, retention_days: int
    ) -> int:
        """PDPA retention (C7): strip PII fields in place, keeping the row
        (with anonymized_at) as the audit trail. Returns rows anonymized."""
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self.async_session() as session:
            result = await session.execute(
                update(TicketModel)
                .where(
                    TicketModel.organization_id == organization_id,
                    TicketModel.created_at < cutoff,
                    TicketModel.anonymized_at.is_(None),
                )
                .values(
                    caller_number="",
                    summary=None,
                    notes=[],
                    anonymized_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return result.rowcount or 0
