"""ccp_tenants table, backfill and service (mt-s1-foundation 2.1–2.4)."""

import asyncio
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from api.db.models import (
    APIKeyModel,
    CcpTenantModel,
    OrganizationConfigurationModel,
    OrganizationModel,
    UserModel,
    organization_users_association,
)
from api.services.ccp import tenants as t

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "b3d1f7a2c9e0_add_ccp_tenants.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("ccp_tenants_mig", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _fresh_orgs(async_session, n: int) -> list[int]:
    """Empty organizations/ccp_tenants inside the test transaction, add n orgs."""
    await async_session.execute(text("DELETE FROM ccp_tenants"))
    await async_session.execute(text("DELETE FROM organization_users"))
    await async_session.execute(text("DELETE FROM api_keys"))
    await async_session.execute(
        text("UPDATE users SET selected_organization_id = NULL")
    )
    await async_session.execute(text("DELETE FROM organizations"))
    ids = []
    for i in range(n):
        org = OrganizationModel(provider_id=f"org_backfill_{i}")
        async_session.add(org)
        await async_session.flush()
        ids.append(org.id)
    return ids


async def _backfill(async_session, env):
    mod = _migration()
    conn = await async_session.connection()
    return mod, await conn.run_sync(lambda c: mod.backfill_ccp_tenants(c, env))


async def _rows(async_session):
    return (
        (
            await async_session.execute(
                select(CcpTenantModel.organization_id, CcpTenantModel.status).order_by(
                    CcpTenantModel.organization_id
                )
            )
        )
        .tuples()
        .all()
    )


# --- backfill ---------------------------------------------------------------


async def test_backfill_zero_organizations(async_session):
    await _fresh_orgs(async_session, 0)
    _, out = await _backfill(async_session, None)
    assert out["rows"] == 0
    assert await _rows(async_session) == []


@pytest.mark.scenario("tenant-isolation:既有部署升級即為第一號租戶")
async def test_backfill_single_organization_is_active(async_session, capsys):
    (org,) = await _fresh_orgs(async_session, 1)
    _, out = await _backfill(async_session, None)
    assert out["rows"] == 1
    assert await _rows(async_session) == [(org, "active")]
    assert "backfilled 1 rows" in capsys.readouterr().err


@pytest.mark.scenario("tenant-isolation:多組織升級只有第一號租戶啟用")
async def test_backfill_three_organizations_designated(async_session, capsys):
    a, b, c = await _fresh_orgs(async_session, 3)
    _, out = await _backfill(async_session, str(b))
    assert await _rows(async_session) == [
        (a, "suspended"),
        (b, "active"),
        (c, "suspended"),
    ]
    assert out["suspended"] == [a, c]
    err = capsys.readouterr().err
    assert str(a) in err and str(c) in err


@pytest.mark.scenario("tenant-isolation:多組織未指定第一號租戶")
@pytest.mark.parametrize("env", [None, "", "abc", "999999"])
async def test_backfill_multiple_unspecified_aborts(async_session, env):
    ids = await _fresh_orgs(async_session, 2)
    mod = _migration()
    conn = await async_session.connection()
    with pytest.raises(mod.TenantBackfillAborted) as exc:
        await conn.run_sync(lambda c: mod.backfill_ccp_tenants(c, env))
    assert all(str(i) in str(exc.value) for i in ids)
    assert await _rows(async_session) == []


# --- DB constraints -----------------------------------------------------------


async def test_status_defaults_to_suspended(async_session):
    (org,) = await _fresh_orgs(async_session, 1)
    await async_session.execute(
        text(
            "INSERT INTO ccp_tenants (organization_id, display_name) VALUES (:o, 'x')"
        ),
        {"o": org},
    )
    assert await _rows(async_session) == [(org, "suspended")]


@pytest.mark.scenario("tenant-isolation:簽收欄位半套被拒")
@pytest.mark.scenario("tenant-isolation:非法狀態值")
@pytest.mark.parametrize(
    "sql",
    [
        (
            "INSERT INTO ccp_tenants (organization_id, display_name, status) "
            "VALUES (:o, 'x', 'trial')"
        ),
        (
            "INSERT INTO ccp_tenants (organization_id, display_name, signed_off_by) "
            "VALUES (:o, 'x', 'someone')"
        ),
        (
            "INSERT INTO ccp_tenants (organization_id, display_name, signed_off_at) "
            "VALUES (:o, 'x', now())"
        ),
        "INSERT INTO ccp_tenants (organization_id, display_name) VALUES (:o, '')",
    ],
    ids=["illegal-status", "half-signoff-by", "half-signoff-at", "empty-name"],
)
async def test_db_rejects(async_session, sql):
    (org,) = await _fresh_orgs(async_session, 1)
    nested = await async_session.begin_nested()
    with pytest.raises(IntegrityError):
        await async_session.execute(text(sql), {"o": org})
    await nested.rollback()


async def test_organization_delete_is_restricted(async_session):
    (org,) = await _fresh_orgs(async_session, 1)
    await _backfill(async_session, None)
    nested = await async_session.begin_nested()
    with pytest.raises(IntegrityError):
        await async_session.execute(
            text("DELETE FROM organizations WHERE id = :o"), {"o": org}
        )
    await nested.rollback()


# --- service ------------------------------------------------------------------


async def _user(async_session, provider_id: str) -> int:
    u = UserModel(provider_id=provider_id)
    async_session.add(u)
    await async_session.flush()
    return u.id


async def _members(async_session, org_id: int) -> set[int]:
    rows = await async_session.execute(
        select(organization_users_association.c.user_id).where(
            organization_users_association.c.organization_id == org_id
        )
    )
    return set(rows.scalars())


async def test_create_tenant_writes_org_key_row_and_members(db_session, async_session):
    await _fresh_orgs(async_session, 0)
    uid = await _user(async_session, "svc-editor")
    res = await t.create_tenant(
        provider_id="ccp-tenant:req-1",
        display_name="Acme 客服",
        status=t.ACTIVE,
        member_user_ids=[uid],
    )
    assert res.created and res.is_tenant
    tenant = await t.get_tenant(res.organization_id)
    assert tenant.status == "active" and tenant.display_name == "Acme 客服"
    assert await _members(async_session, res.organization_id) == {uid}
    key = (
        await async_session.execute(
            select(APIKeyModel).where(
                APIKeyModel.organization_id == res.organization_id
            )
        )
    ).scalar_one()
    assert key.created_by is None


async def test_create_tenant_resend_restores_deleted_member(db_session, async_session):
    await _fresh_orgs(async_session, 0)
    uid = await _user(async_session, "svc-editor")
    first = await t.create_tenant(
        provider_id="ccp-tenant:req-2",
        display_name="Beta",
        status=t.ACTIVE,
        member_user_ids=[uid],
    )
    await async_session.execute(
        organization_users_association.delete().where(
            organization_users_association.c.organization_id == first.organization_id
        )
    )
    again = await t.create_tenant(
        provider_id="ccp-tenant:req-2",
        display_name="Beta",
        status=t.ACTIVE,
        member_user_ids=[uid],
    )
    assert again.organization_id == first.organization_id
    assert not again.created and again.is_tenant
    assert await _members(async_session, first.organization_id) == {uid}
    keys = (
        await async_session.execute(
            select(func.count())
            .select_from(APIKeyModel)
            .where(APIKeyModel.organization_id == first.organization_id)
        )
    ).scalar_one()
    assert keys == 1


async def test_create_tenant_never_absorbs_non_tenant_org(db_session, async_session):
    (org,) = await _fresh_orgs(async_session, 1)
    uid = await _user(async_session, "svc-editor")
    res = await t.create_tenant(
        provider_id="org_backfill_0",
        display_name="Hijack",
        status=t.ACTIVE,
        member_user_ids=[uid],
    )
    assert res == t.CreateTenantResult(org, created=False, is_tenant=False)
    assert not await t.is_tenant(org)
    assert await _members(async_session, org) == set()


@pytest.mark.parametrize("variant", ["ACME", " acme ", "ＡＣＭＥ"])
async def test_display_name_normalised_unique(db_session, async_session, variant):
    await _fresh_orgs(async_session, 0)
    await t.create_tenant(
        provider_id="ccp-tenant:a", display_name="Acme", status=t.ACTIVE
    )
    with pytest.raises(t.DisplayNameTaken):
        await t.create_tenant(
            provider_id="ccp-tenant:b", display_name=variant, status=t.ACTIVE
        )
    orgs = (
        await async_session.execute(
            select(func.count())
            .select_from(OrganizationModel)
            .where(OrganizationModel.provider_id == "ccp-tenant:b")
        )
    ).scalar_one()
    assert orgs == 0


async def test_create_tenant_requires_explicit_valid_status(db_session):
    with pytest.raises(ValueError):
        await t.create_tenant(
            provider_id="ccp-tenant:x", display_name="x", status="trial"
        )


async def test_ensure_first_tenant_empty_then_nonempty(db_session, async_session):
    a, b = await _fresh_orgs(async_session, 2)
    assert await t.ensure_first_tenant(a) is True
    assert (await t.get_tenant(a)).status == "active"
    assert await t.ensure_first_tenant(b) is False
    assert not await t.is_tenant(b)
    assert await t.count_tenants() == 1


@pytest.mark.scenario("tenant-isolation:停用保留資料")
async def test_suspend_keeps_data_and_updates_timestamp(db_session, async_session):
    await _fresh_orgs(async_session, 0)
    res = await t.create_tenant(
        provider_id="ccp-tenant:s", display_name="S", status=t.ACTIVE
    )
    org = res.organization_id
    async_session.add(
        OrganizationConfigurationModel(
            organization_id=org, key="CCP_SYSTEM_NAME", value={"v": "x"}
        )
    )
    await async_session.flush()
    await async_session.execute(
        text(
            "UPDATE ccp_tenants SET status_changed_at = now() - interval '1 day' "
            "WHERE organization_id = :o"
        ),
        {"o": org},
    )
    before = (await t.get_tenant(org)).status_changed_at
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), format="{message} {extra}")
    try:
        assert await t.set_tenant_status(org, t.SUSPENDED, actor="implementer:7")
    finally:
        logger.remove(sink)
    after = await t.get_tenant(org)
    assert after.status == "suspended" and after.status_changed_at > before
    cfg = (
        await async_session.execute(
            select(func.count())
            .select_from(OrganizationConfigurationModel)
            .where(OrganizationConfigurationModel.organization_id == org)
        )
    ).scalar_one()
    assert cfg == 1
    log = " ".join(lines)
    assert "ccp_tenant.status_changed" in log and "'old_status': 'active'" in log


@pytest.mark.scenario("tenant-isolation:非法狀態值")
async def test_set_status_rejects_illegal_and_unknown(db_session, async_session):
    await _fresh_orgs(async_session, 0)
    with pytest.raises(ValueError):
        await t.set_tenant_status(1, "trial", actor="x")
    assert await t.set_tenant_status(987654, t.ACTIVE, actor="x") is False


async def test_record_signoff(db_session, async_session):
    (org,) = await _fresh_orgs(async_session, 1)
    await t.ensure_first_tenant(org)
    at = datetime(2026, 11, 24, tzinfo=UTC)
    assert await t.record_signoff(org, signed_off_by="implementer:7", signed_off_at=at)
    tenant = await t.get_tenant(org)
    assert tenant.signed_off_by == "implementer:7" and tenant.signed_off_at == at


@pytest.mark.scenario("tenant-isolation:非租戶組織")
async def test_non_tenant_organization(db_session, async_session):
    (org,) = await _fresh_orgs(async_session, 1)
    assert await t.get_tenant(org) is None
    assert await t.is_tenant(org) is False


async def test_create_logs_carry_no_display_name(db_session, async_session):
    await _fresh_orgs(async_session, 0)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), format="{message} {extra}")
    try:
        await t.create_tenant(
            provider_id="ccp-tenant:pii", display_name="王小明診所", status=t.ACTIVE
        )
    finally:
        logger.remove(sink)
    assert lines and "王小明診所" not in " ".join(lines)


# --- concurrency (real commits; cleans up after itself) ------------------------


async def test_ensure_first_tenant_concurrent_creates_one_row(test_engine, monkeypatch):
    # Own session maker on the test engine: the two calls need real, separate
    # connections, and must not inherit a session maker an earlier test left
    # bound to a closed connection.
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from api.db import db_client

    monkeypatch.setattr(db_client, "async_session", async_sessionmaker(test_engine))
    async with test_engine.begin() as conn:
        if (await conn.execute(text("SELECT count(*) FROM ccp_tenants"))).scalar():
            pytest.skip("test DB ccp_tenants not empty")
        ids = [
            (
                await conn.execute(
                    text(
                        "INSERT INTO organizations (provider_id, created_at) "
                        "VALUES (:p, now()) RETURNING id"
                    ),
                    {"p": f"org_concurrent_{i}"},
                )
            ).scalar_one()
            for i in range(2)
        ]
    try:
        results = await asyncio.gather(*(t.ensure_first_tenant(i) for i in ids))
        assert sorted(results) == [False, True]
        async with test_engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT count(*) FROM ccp_tenants"))
            ).scalar() == 1
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM ccp_tenants WHERE organization_id = ANY(:i)"),
                {"i": ids},
            )
            await conn.execute(
                text("DELETE FROM organizations WHERE id = ANY(:i)"), {"i": ids}
            )
