"""telephony_phone_numbers.address_normalized unique platform-wide (mt-s1-foundation)

Pre-check: any number held by more than one row aborts the upgrade and lists
it (last four digits only) with the organization ids. Nothing is deleted or
reassigned. The per-organization unique constraint becomes a plain index.

Revision ID: c5e2a8d4f1b6
Revises: b3d1f7a2c9e0
Create Date: 2026-11-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5e2a8d4f1b6"
down_revision: Union[str, None] = "b3d1f7a2c9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class DuplicatePhoneNumbers(RuntimeError):
    pass


def duplicate_numbers(conn) -> list[tuple[str, list[int]]]:
    rows = conn.execute(
        sa.text(
            "SELECT address_normalized, array_agg(DISTINCT organization_id "
            "ORDER BY organization_id) AS orgs "
            "FROM telephony_phone_numbers GROUP BY address_normalized "
            "HAVING count(*) > 1 ORDER BY address_normalized"
        )
    ).all()
    return [("***" + r.address_normalized[-4:], list(r.orgs)) for r in rows]


def check_no_duplicates(conn) -> None:
    dups = duplicate_numbers(conn)
    if dups:
        listed = "; ".join(f"{masked} organizations {orgs}" for masked, orgs in dups)
        raise DuplicatePhoneNumbers(
            f"{len(dups)} phone number(s) registered more than once: {listed}. "
            "Resolve by hand (RUNBOOK) and rerun; nothing was changed."
        )


def upgrade() -> None:
    check_no_duplicates(op.get_bind())
    op.drop_constraint(
        "uq_phone_numbers_org_address", "telephony_phone_numbers", type_="unique"
    )
    op.create_index(
        "ix_phone_numbers_org_address",
        "telephony_phone_numbers",
        ["organization_id", "address_normalized"],
    )
    op.create_index(
        "uq_phone_numbers_address_global",
        "telephony_phone_numbers",
        ["address_normalized"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_phone_numbers_address_global", table_name="telephony_phone_numbers"
    )
    op.drop_index("ix_phone_numbers_org_address", table_name="telephony_phone_numbers")
    op.create_unique_constraint(
        "uq_phone_numbers_org_address",
        "telephony_phone_numbers",
        ["organization_id", "address_normalized"],
    )
