"""workflow_runs.organization_id: add, backfill from workflow, NOT NULL (mt-s1-foundation)

Rollback order (RUNBOOK): downgrade past this revision *before* deploying code
that does not write ``workflow_runs.organization_id``.

Revision ID: e9b5d3f0a726
Revises: d4a7c2e9b815
Create Date: 2026-11-24
"""

import sys
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9b5d3f0a726"
down_revision: Union[str, None] = "d4a7c2e9b815"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def backfill_run_organizations(conn) -> int:
    n = conn.execute(
        sa.text(
            "UPDATE workflow_runs r SET organization_id = w.organization_id "
            "FROM workflows w WHERE w.id = r.workflow_id "
            "AND r.organization_id IS NULL"
        )
    ).rowcount
    print(
        f"[workflow_runs.organization_id backfill] backfilled {n} rows",
        file=sys.stderr,
        flush=True,
    )
    return n


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", name="fk_workflow_runs_organization_id"),
            nullable=True,
        ),
    )
    backfill_run_organizations(op.get_bind())
    op.alter_column("workflow_runs", "organization_id", nullable=False)
    op.create_index(
        "idx_workflow_runs_org_created",
        "workflow_runs",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_workflow_runs_org_created", table_name="workflow_runs")
    op.drop_constraint(
        "fk_workflow_runs_organization_id", "workflow_runs", type_="foreignkey"
    )
    op.drop_column("workflow_runs", "organization_id")
