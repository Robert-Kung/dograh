"""clear workflows.call_disposition_codes (W4b usage report)

The list was auto-accumulated from each call's mapped_call_disposition, which
can be LLM free text carrying caller PII; the call end no longer writes it.
Data-only and irreversible: the cleared values are not restored on downgrade.

Revision ID: d7a3e9c41b52
Revises: c81f2ab04d55
Create Date: 2026-10-06
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d7a3e9c41b52"
down_revision: Union[str, None] = "c81f2ab04d55"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE workflows SET call_disposition_codes = '{}'::json")


def downgrade() -> None:
    pass
