"""CCP tenants (mt-s1-foundation, tenant-isolation).

A dograh organization is a CCP tenant iff ``ccp_tenants`` has its row. This
module is the only writer of ``ccp_tenants``; routes and CLIs wrap it.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from api.db import db_client
from api.db.models import (
    APIKeyModel,
    CcpTenantModel,
    OrganizationModel,
    organization_users_association,
)
from api.utils.api_key import generate_api_key

ACTIVE = "active"
SUSPENDED = "suspended"
STATUSES = (ACTIVE, SUSPENDED)


class DisplayNameTaken(Exception):
    """Another tenant already uses this display name (after normalisation)."""


class OrganizationNotFound(Exception):
    """The organization id does not exist."""


@dataclass(frozen=True)
class Tenant:
    organization_id: int
    display_name: str
    status: str
    status_changed_at: datetime
    signed_off_by: str | None
    signed_off_at: datetime | None


@dataclass(frozen=True)
class CreateTenantResult:
    organization_id: int
    # True when this call inserted the organization (and its tenant row).
    created: bool
    # False when the provider_id already belonged to a non-tenant organization;
    # such an organization is never absorbed into ccp_tenants here.
    is_tenant: bool


def display_name_key(name: str) -> str:
    """NFKC, trim, casefold — two names with the same key are the same name."""
    return unicodedata.normalize("NFKC", name).strip().casefold()[:64]


def _to_tenant(row: CcpTenantModel) -> Tenant:
    return Tenant(
        organization_id=row.organization_id,
        display_name=row.display_name,
        status=row.status,
        status_changed_at=row.status_changed_at,
        signed_off_by=row.signed_off_by,
        signed_off_at=row.signed_off_at,
    )


async def get_tenant(organization_id: int) -> Tenant | None:
    """The tenant row, or None when the organization is not a CCP tenant."""
    async with db_client.async_session() as session:
        row = await session.get(CcpTenantModel, organization_id)
        return _to_tenant(row) if row is not None else None


async def is_tenant(organization_id: int) -> bool:
    return await get_tenant(organization_id) is not None


async def count_tenants() -> int:
    """All ccp_tenants rows, suspended included."""
    async with db_client.async_session() as session:
        return (
            await session.execute(select(func.count()).select_from(CcpTenantModel))
        ).scalar_one()


async def create_tenant(
    *,
    provider_id: str,
    display_name: str,
    status: str,
    member_user_ids: Iterable[int] = (),
) -> CreateTenantResult:
    """Create organization + default API key + tenant row + members, atomically.

    One session, one commit. Does not call the upstream helpers that commit on
    their own (``get_or_create_organization_by_provider_id`` /
    ``add_user_to_organization``). Re-sending for an organization that already
    exists is idempotent: when it is a tenant, its members are ensured again;
    when it is not, nothing is written.
    """
    if status not in STATUSES:
        raise ValueError(f"invalid tenant status: {status!r}")
    members = sorted(set(member_user_ids))
    async with db_client.async_session() as session:
        inserted = await session.execute(
            insert(OrganizationModel.__table__)
            .values(provider_id=provider_id, created_at=datetime.now(UTC))
            .on_conflict_do_nothing(index_elements=["provider_id"])
            .returning(OrganizationModel.__table__.c.id)
        )
        new_id = inserted.scalar_one_or_none()
        if new_id is None:
            org_id = (
                await session.execute(
                    select(OrganizationModel.id).where(
                        OrganizationModel.provider_id == provider_id
                    )
                )
            ).scalar_one()
            tenant = await session.get(CcpTenantModel, org_id)
            if tenant is None:
                return CreateTenantResult(org_id, created=False, is_tenant=False)
            await _ensure_members(session, org_id, members)
            await session.commit()
            return CreateTenantResult(org_id, created=False, is_tenant=True)

        _, key_hash, key_prefix = generate_api_key()
        session.add(
            APIKeyModel(
                organization_id=new_id,
                name="Default API Key",
                key_hash=key_hash,
                key_prefix=key_prefix,
                is_active=True,
                created_by=None,
            )
        )
        session.add(
            CcpTenantModel(
                organization_id=new_id,
                display_name=display_name,
                display_name_key=display_name_key(display_name),
                status=status,
            )
        )
        try:
            await session.flush()
        except IntegrityError as exc:
            # Discard the organization and key inserted above as well.
            await session.rollback()
            if "uq_ccp_tenants_display_name_key" in str(exc.orig):
                raise DisplayNameTaken() from None
            raise
        await _ensure_members(session, new_id, members)
        await session.commit()
    logger.bind(organization_id=new_id, status=status).info("ccp_tenant.created")
    return CreateTenantResult(new_id, created=True, is_tenant=True)


async def _ensure_members(session, organization_id: int, user_ids: list[int]) -> None:
    if not user_ids:
        return
    await session.execute(
        insert(organization_users_association)
        .values([{"user_id": u, "organization_id": organization_id} for u in user_ids])
        .on_conflict_do_nothing()
    )


async def ensure_first_tenant(organization_id: int) -> bool:
    """Make ``organization_id`` the active first tenant iff ccp_tenants is empty.

    The emptiness check and the insert are serialised with a table lock, so two
    concurrent callers create at most one row. Returns whether a row was made;
    raises ``OrganizationNotFound`` for an unknown id.
    """
    async with db_client.async_session() as session:
        await session.execute(
            text("LOCK TABLE ccp_tenants IN SHARE ROW EXCLUSIVE MODE")
        )
        existing = (
            await session.execute(select(func.count()).select_from(CcpTenantModel))
        ).scalar_one()
        if existing:
            return False
        provider_id = (
            await session.execute(
                select(OrganizationModel.provider_id).where(
                    OrganizationModel.id == organization_id
                )
            )
        ).scalar_one_or_none()
        if provider_id is None:
            raise OrganizationNotFound(organization_id)
        name = provider_id[:64]
        session.add(
            CcpTenantModel(
                organization_id=organization_id,
                display_name=name,
                display_name_key=display_name_key(name),
                status=ACTIVE,
            )
        )
        await session.commit()
    logger.bind(organization_id=organization_id).info("ccp_tenant.first_tenant_created")
    return True


async def set_tenant_status(organization_id: int, status: str, *, actor: str) -> bool:
    """Change a tenant's status; data is never touched. Returns False if no row."""
    if status not in STATUSES:
        raise ValueError(f"invalid tenant status: {status!r}")
    async with db_client.async_session() as session:
        row = await session.get(CcpTenantModel, organization_id, with_for_update=True)
        if row is None:
            return False
        old = row.status
        await session.execute(
            update(CcpTenantModel)
            .where(CcpTenantModel.organization_id == organization_id)
            .values(status=status, status_changed_at=func.now())
        )
        await session.commit()
    logger.bind(
        organization_id=organization_id, old_status=old, new_status=status, actor=actor
    ).info("ccp_tenant.status_changed")
    return True


async def record_signoff(
    organization_id: int, *, signed_off_by: str, signed_off_at: datetime
) -> bool:
    """Record the sign-off (both fields together). Returns False if no row."""
    async with db_client.async_session() as session:
        result = await session.execute(
            update(CcpTenantModel)
            .where(CcpTenantModel.organization_id == organization_id)
            .values(signed_off_by=signed_off_by, signed_off_at=signed_off_at)
        )
        await session.commit()
    return result.rowcount > 0
