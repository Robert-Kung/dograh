"""add tickets table

Built-in ticket MCP server store (S-L4-SCREENPOP). Org-scoped, idempotent
on (organization_id, workflow_run_id), caller-number lookup index for the
screen-pop fallback channel.

Revision ID: b3d47a1c9e02
Revises: 91cc6ba3e1c7
Create Date: 2026-07-03 09:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d47a1c9e02"
down_revision: Union[str, None] = "91cc6ba3e1c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.String(), nullable=False),
        sa.Column("workflow_run_id", sa.Integer(), nullable=False),
        sa.Column("caller_number", sa.String(), nullable=False, server_default=""),
        sa.Column("room_name", sa.String(), nullable=False, server_default=""),
        sa.Column("transfer_reason", sa.String(), nullable=False, server_default=""),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "workflow_run_id", name="_ticket_org_run_uc"
        ),
        sa.UniqueConstraint("organization_id", "ticket_id", name="_ticket_org_tid_uc"),
    )
    op.create_index("ix_tickets_id", "tickets", ["id"])
    op.create_index(
        "ix_tickets_org_caller", "tickets", ["organization_id", "caller_number"]
    )


def downgrade() -> None:
    op.drop_index("ix_tickets_org_caller", table_name="tickets")
    op.drop_index("ix_tickets_id", table_name="tickets")
    op.drop_table("tickets")
