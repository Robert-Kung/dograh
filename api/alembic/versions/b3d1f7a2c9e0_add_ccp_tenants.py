"""add ccp_tenants (mt-s1-foundation, tenant-isolation)

A dograh organization is a CCP tenant iff it has a row here. Backfill:

- 0 organizations  -> 0 rows (first tenant is created by the signup exception).
- 1 organization   -> that one, ``active``.
- >1 organizations -> ``CCP_FIRST_TENANT_ORGANIZATION_ID`` names the ``active``
  one, every other organization is ``suspended`` and listed; unset (or naming
  an organization that does not exist) aborts and lists all organization ids.

Never backfills everything as ``active`` (fail-open).

Revision ID: b3d1f7a2c9e0
Revises: f1c5a9e3b284
Create Date: 2026-11-24
"""

import os
import sys
import unicodedata
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3d1f7a2c9e0"
down_revision: Union[str, None] = "f1c5a9e3b284"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FIRST_TENANT_ENV = "CCP_FIRST_TENANT_ORGANIZATION_ID"


class TenantBackfillAborted(RuntimeError):
    pass


def _display_name_key(name: str) -> str:
    # Same normalisation as api.services.ccp.tenants.display_name_key; copied so
    # this revision stays frozen if the service evolves.
    return unicodedata.normalize("NFKC", name).strip().casefold()[:64]


def _emit(message: str) -> None:
    print(f"[ccp_tenants backfill] {message}", file=sys.stderr, flush=True)


def backfill_ccp_tenants(conn, first_tenant_env: str | None) -> dict:
    """Insert one ccp_tenants row per existing organization.

    Returns ``{"rows": n, "active": id|None, "suspended": [ids]}``; raises
    ``TenantBackfillAborted`` without writing when the first tenant cannot be
    determined.
    """
    orgs = conn.execute(
        sa.text("SELECT id, provider_id FROM organizations ORDER BY id")
    ).all()
    if not orgs:
        _emit("backfilled 0 rows (no organizations)")
        return {"rows": 0, "active": None, "suspended": []}

    ids = [row.id for row in orgs]
    if len(orgs) == 1:
        active_id = ids[0]
    else:
        raw = (first_tenant_env or "").strip()
        if not raw.isdigit() or int(raw) not in ids:
            raise TenantBackfillAborted(
                f"{len(ids)} organizations exist and {FIRST_TENANT_ENV} does not "
                f"name one of them; organization ids: {ids}. Write "
                "deploy/.runtime/first-tenant-organization-id (RUNBOOK) and rerun."
            )
        active_id = int(raw)

    suspended = [i for i in ids if i != active_id]
    for row in orgs:
        name = row.provider_id[:64]
        conn.execute(
            sa.text(
                "INSERT INTO ccp_tenants "
                "(organization_id, display_name, display_name_key, status) "
                "VALUES (:id, :name, :key, :status)"
            ),
            {
                "id": row.id,
                "name": name,
                "key": _display_name_key(name),
                "status": "active" if row.id == active_id else "suspended",
            },
        )
    _emit(f"backfilled {len(orgs)} rows; active organization {active_id}")
    if suspended:
        _emit(f"backfilled as suspended (confirm each, RUNBOOK): {suspended}")
    return {"rows": len(orgs), "active": active_id, "suspended": suspended}


def upgrade() -> None:
    op.create_table(
        "ccp_tenants",
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("display_name_key", sa.String(64), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'suspended'"),
        ),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("signed_off_by", sa.String(128), nullable=True),
        sa.Column("signed_off_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name="ccp_tenants_status_valid"
        ),
        sa.CheckConstraint(
            "(signed_off_by IS NULL) = (signed_off_at IS NULL)",
            name="ccp_tenants_signoff_both_or_neither",
        ),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 64",
            name="ccp_tenants_display_name_length",
        ),
    )
    op.create_index(
        "uq_ccp_tenants_display_name_key",
        "ccp_tenants",
        ["display_name_key"],
        unique=True,
        postgresql_where=sa.text("display_name_key IS NOT NULL"),
    )
    backfill_ccp_tenants(op.get_bind(), os.environ.get(FIRST_TENANT_ENV))


def downgrade() -> None:
    op.drop_index("uq_ccp_tenants_display_name_key", table_name="ccp_tenants")
    op.drop_table("ccp_tenants")
