"""add recording_retention_audit.scope (W4c split retention)

Nullable: rows written before the audio/transcript split stay NULL and are
read by their object key prefix.

Revision ID: f1c5a9e3b284
Revises: e4b8c2d17a60
Create Date: 2026-10-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1c5a9e3b284"
down_revision: Union[str, None] = "e4b8c2d17a60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "recording_retention_audit", sa.Column("scope", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("recording_retention_audit", "scope")
