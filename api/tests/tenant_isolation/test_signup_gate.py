"""Public signup is closed; the deploy secret is the one exception and the
signup that finds ``ccp_tenants`` empty creates the first tenant
(mt-s1-foundation 6.1 / 6.2 / 6.4).

tenant-isolation〈關閉公開註冊〉〈租戶定義與 CCP 租戶表〉.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from api.db.models import CcpTenantModel, OrganizationModel, UserModel
from api.services.ccp import tenants

URL = "/api/v1/auth/signup"
SECRET = "test-devops-secret"


@pytest.fixture(autouse=True)
def devops_secret(monkeypatch):
    monkeypatch.setattr("api.constants.DOGRAH_DEVOPS_SECRET", SECRET)


@pytest.fixture(autouse=True)
def no_mps(monkeypatch):
    async def none(*_a, **_k):
        return None

    monkeypatch.setattr("api.routes.auth.create_user_configuration_with_mps_key", none)


def _body(tag: str) -> dict:
    return {
        "email": f"{tag}@signup-gate.example.com",
        "password": "pw-123456789",
        "name": tag,
    }


async def _count(async_session, model) -> int:
    return (
        await async_session.execute(select(func.count()).select_from(model))
    ).scalar_one()


@pytest.fixture
async def client(test_client_factory, db_session):
    anyone, _ = await db_session.get_or_create_user_by_provider_id("signup_probe")
    async with test_client_factory(anyone) as c:
        yield c


@pytest.mark.scenario("tenant-isolation:匿名註冊被拒")
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Dograh-Devops-Secret": "wrong"},
        {"X-Dograh-Devops-Secret": "test-devops-secrét".encode()},
        {"X-Dograh-Call-Records-Secret": SECRET},
    ],
)
async def test_anonymous_signup_is_refused_without_writes(
    client, async_session, headers
):
    users, orgs = (
        await _count(async_session, UserModel),
        await _count(async_session, OrganizationModel),
    )
    r = await client.post(URL, json=_body("anon"), headers=headers)
    assert (r.status_code, r.json()) == (403, {"detail": "signup_disabled"})
    assert await _count(async_session, UserModel) == users
    assert await _count(async_session, OrganizationModel) == orgs


@pytest.mark.scenario("tenant-isolation:匿名註冊被拒")
async def test_unconfigured_secret_refuses_signup(client, monkeypatch):
    monkeypatch.setattr("api.constants.DOGRAH_DEVOPS_SECRET", "")
    r = await client.post(
        URL, json=_body("unconf"), headers={"X-Dograh-Devops-Secret": ""}
    )
    assert (r.status_code, r.json()) == (403, {"detail": "signup_disabled"})


async def _empty_tenants(async_session):
    await async_session.execute(text("DELETE FROM ccp_tenants"))


@pytest.mark.scenario("tenant-isolation:空 DB 首啟建立第一號租戶")
async def test_secret_signup_on_empty_table_creates_active_first_tenant(
    client, async_session
):
    await _empty_tenants(async_session)
    r = await client.post(
        URL, json=_body("first"), headers={"X-Dograh-Devops-Secret": SECRET}
    )
    assert r.status_code == 200
    org_id = r.json()["user"]["organization_id"]
    tenant = await tenants.get_tenant(org_id)
    assert tenant is not None and tenant.status == "active"
    assert await tenants.is_tenant(org_id)


@pytest.mark.scenario("tenant-isolation:空 DB 首啟建立第一號租戶")
async def test_secret_signup_on_non_empty_table_is_not_a_tenant(client, async_session):
    await _empty_tenants(async_session)
    first = await client.post(
        URL, json=_body("first2"), headers={"X-Dograh-Devops-Secret": SECRET}
    )
    second = await client.post(
        URL, json=_body("second2"), headers={"X-Dograh-Devops-Secret": SECRET}
    )
    assert first.status_code == second.status_code == 200
    assert await tenants.is_tenant(first.json()["user"]["organization_id"])
    assert not await tenants.is_tenant(second.json()["user"]["organization_id"])
    assert await _count(async_session, CcpTenantModel) == 1


@pytest.mark.scenario("tenant-isolation:bootstrap 重跑冪等")
async def test_rerun_is_409_then_login(client, async_session):
    await _empty_tenants(async_session)
    h = {"X-Dograh-Devops-Secret": SECRET}
    assert (await client.post(URL, json=_body("rerun"), headers=h)).status_code == 200
    again = await client.post(URL, json=_body("rerun"), headers=h)
    assert again.status_code == 409
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "rerun@signup-gate.example.com", "password": "pw-123456789"},
    )
    assert login.status_code == 200


@pytest.mark.scenario("tenant-isolation:空 DB 首啟建立第一號租戶")
async def test_empty_db_first_tenant_can_take_calls_and_read_reports(
    test_client_factory, db_session, async_session, monkeypatch
):
    """6.4 空 DB 首啟：signup 建出的第一號租戶可收電話、服務間報表不回 403."""
    from unittest.mock import AsyncMock, patch

    await _empty_tenants(async_session)
    probe, _ = await db_session.get_or_create_user_by_provider_id("signup_probe2")
    async with test_client_factory(probe) as anon:
        r = await anon.post(
            URL, json=_body("boot"), headers={"X-Dograh-Devops-Secret": SECRET}
        )
    assert r.status_code == 200
    org_id, user_id = r.json()["user"]["organization_id"], r.json()["user"]["id"]
    user = await db_session.get_user_by_id(user_id)
    wf = await db_session.create_workflow(
        name="boot reception",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org_id,
    )
    with patch(
        "api.routes.organization._sync_inbound_for_phone_number", new_callable=AsyncMock
    ):
        async with test_client_factory(user) as client:
            mapped = await client.post(
                "/api/v1/livekit/did-mappings",
                json={"address": "+886255550199", "workflow_id": wf.id},
            )
    assert mapped.status_code == 201
    resolved = await db_session.find_inbound_workflow_for_did("+886255550199")
    assert (resolved.mapped, resolved.organization_id) == (True, org_id)

    monkeypatch.setattr("api.constants.DOGRAH_DEVOPS_SECRET", SECRET)
    async with test_client_factory(user) as client:
        report = await client.get(
            "/api/v1/internal/usage-report",
            params={
                "from": "2021-03-01",
                "to": "2021-03-02",
                "timezone": "Asia/Taipei",
            },
            headers={
                "X-Dograh-Devops-Secret": SECRET,
                "X-CCP-Organization-Id": str(org_id),
            },
        )
    assert report.status_code == 200
