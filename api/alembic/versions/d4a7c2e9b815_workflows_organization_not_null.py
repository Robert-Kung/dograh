"""workflows.organization_id backfilled and NOT NULL (mt-s1-foundation)

Backfill for rows where ``organization_id`` is NULL, in order:

1. the organization of the phone number(s) routing inbound calls to it;
2. the creator's organization, when the creator belongs to exactly one.

No candidate, phone numbers in more than one organization, or a phone-number
organization the creator does not belong to (when the creator belongs to any
organization) aborts the upgrade and lists the workflow ids. Never uses the creator's
``selected_organization_id``.

Revision ID: d4a7c2e9b815
Revises: c5e2a8d4f1b6
Create Date: 2026-11-24
"""

import sys
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4a7c2e9b815"
down_revision: Union[str, None] = "c5e2a8d4f1b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class WorkflowBackfillAborted(RuntimeError):
    pass


def _emit(message: str) -> None:
    print(
        f"[workflows.organization_id backfill] {message}", file=sys.stderr, flush=True
    )


def backfill_workflow_organizations(conn) -> dict:
    """Fill NULL ``workflows.organization_id``; raise without writing if any
    workflow cannot be attributed. Returns ``{"rows": n}``."""
    rows = conn.execute(
        sa.text(
            """
            SELECT w.id,
                   (SELECT array_agg(DISTINCT p.organization_id)
                      FROM telephony_phone_numbers p
                     WHERE p.inbound_workflow_id = w.id) AS phone_orgs,
                   (SELECT array_agg(ou.organization_id)
                      FROM organization_users ou
                     WHERE ou.user_id = w.user_id) AS creator_orgs
              FROM workflows w
             WHERE w.organization_id IS NULL
             ORDER BY w.id
            """
        )
    ).all()

    assign: dict[int, int] = {}
    unresolved: list[int] = []
    for row in rows:
        phone = set(row.phone_orgs or [])
        creator = set(row.creator_orgs or [])
        creator_single = next(iter(creator)) if len(creator) == 1 else None
        if len(phone) > 1:
            unresolved.append(row.id)
        elif phone:
            org = next(iter(phone))
            if creator and org not in creator:
                unresolved.append(row.id)
            else:
                assign[row.id] = org
        elif creator_single is not None:
            assign[row.id] = creator_single
        else:
            unresolved.append(row.id)

    if unresolved:
        raise WorkflowBackfillAborted(
            f"{len(unresolved)} workflow(s) cannot be attributed to one "
            f"organization: workflow ids {unresolved}. Set organization_id by "
            "hand (RUNBOOK) and rerun; nothing was changed."
        )

    for wf_id, org_id in assign.items():
        conn.execute(
            sa.text("UPDATE workflows SET organization_id = :o WHERE id = :w"),
            {"o": org_id, "w": wf_id},
        )
    _emit(f"backfilled {len(assign)} rows")
    return {"rows": len(assign)}


def upgrade() -> None:
    backfill_workflow_organizations(op.get_bind())
    op.alter_column("workflows", "organization_id", nullable=False)


def downgrade() -> None:
    op.alter_column("workflows", "organization_id", nullable=True)
