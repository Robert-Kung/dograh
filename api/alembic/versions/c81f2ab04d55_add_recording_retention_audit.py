"""add recording_retention_audit (S-L8-RECORD)

Revision ID: c81f2ab04d55
Revises: b3d47a1c9e02
Create Date: 2026-07-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c81f2ab04d55"
down_revision: Union[str, None] = "b3d47a1c9e02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recording_retention_audit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workflow_run_id", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("object_keys", sa.JSON(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_recording_retention_audit_id"),
        "recording_retention_audit",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recording_retention_audit_workflow_run_id"),
        "recording_retention_audit",
        ["workflow_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_recording_retention_audit_workflow_run_id"),
        table_name="recording_retention_audit",
    )
    op.drop_index(
        op.f("ix_recording_retention_audit_id"), table_name="recording_retention_audit"
    )
    op.drop_table("recording_retention_audit")
