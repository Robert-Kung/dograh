"""add ccp_call_meta and ccp_settings (W4c call records)

Revision ID: e4b8c2d17a60
Revises: d7a3e9c41b52
Create Date: 2026-10-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4b8c2d17a60"
down_revision: Union[str, None] = "d7a3e9c41b52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ccp_call_meta",
        sa.Column("workflow_run_id", sa.Integer(), nullable=False),
        sa.Column("caller_masked", sa.String(length=7), nullable=True),
        sa.Column("caller_last4", sa.String(length=4), nullable=True),
        sa.Column("caller_hmac", sa.String(length=64), nullable=True),
        sa.Column("audio_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workflow_run_id"),
    )
    op.create_index(
        op.f("ix_ccp_call_meta_caller_hmac"),
        "ccp_call_meta",
        ["caller_hmac"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ccp_call_meta_caller_last4"),
        "ccp_call_meta",
        ["caller_last4"],
        unique=False,
    )
    op.create_table(
        "ccp_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("key_fingerprint", sa.String(length=8), nullable=True),
        sa.CheckConstraint("id = 1", name="ccp_settings_single_row"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("ccp_settings")
    op.drop_index(op.f("ix_ccp_call_meta_caller_last4"), table_name="ccp_call_meta")
    op.drop_index(op.f("ix_ccp_call_meta_caller_hmac"), table_name="ccp_call_meta")
    op.drop_table("ccp_call_meta")
