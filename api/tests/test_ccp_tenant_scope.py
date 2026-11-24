"""CCP call records and usage report are scoped to one organization
(mt-s1-foundation 5.1 / 5.5).

tenant-isolation〈服務間請求攜帶組織編號〉, call-records〈通話紀錄以組織為範圍〉,
usage-report〈用量報表以組織為範圍〉. Real test DB, real routes; rows in 2021 so
nothing else falls into the queried range.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from api.db.models import CcpTenantModel, WorkflowRunModel

CR = "/api/v1/internal/call-records"
UR = "/api/v1/internal/usage-report"
CR_SECRET = "test-call-records-secret"
DEVOPS = "test-devops-secret"
ORG_HEADER = "X-CCP-Organization-Id"
BODY = {"from": "2021-03-01", "to": "2021-03-31", "timezone": "Asia/Taipei"}
USAGE = {"from": "2021-03-01", "to": "2021-03-31", "timezone": "Asia/Taipei"}


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setattr("api.constants.DOGRAH_CALL_RECORDS_SECRET", CR_SECRET)
    monkeypatch.setattr("api.constants.DOGRAH_DEVOPS_SECRET", DEVOPS)


async def _tenant(db_session, async_session, tag: str):
    user, _ = await db_session.get_or_create_user_by_provider_id(f"scope_u_{tag}")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"scope_org_{tag}", user.id
    )
    async_session.add(
        CcpTenantModel(
            organization_id=org.id, display_name=f"scope {tag}", status="active"
        )
    )
    wf = await db_session.create_workflow(
        name=f"scope {tag}",
        workflow_definition={"nodes": [], "edges": []},
        user_id=None,
        organization_id=org.id,
    )
    run = WorkflowRunModel(
        name=f"scope run {tag}",
        workflow_id=wf.id,
        organization_id=org.id,
        mode="livekit",
        call_type="inbound",
        is_completed=True,
        created_at=datetime(2021, 3, 10, 2, tzinfo=UTC),
        annotations={"call_outcome": "ai_completed"},
        usage_info={"call_duration_seconds": 42},
        initial_context={"did": "+886212345678"},
        storage_backend="minio",
    )
    async_session.add(run)
    await async_session.flush()
    return user, org, run


@pytest.fixture
async def two_tenants(db_session, async_session):
    ua, a, run_a = await _tenant(db_session, async_session, "a")
    _, b, run_b = await _tenant(db_session, async_session, "b")
    return ua, a, run_a, b, run_b


def _cr(org_id):
    return {"X-Dograh-Call-Records-Secret": CR_SECRET, ORG_HEADER: str(org_id)}


def _ur(org_id):
    return {"X-Dograh-Devops-Secret": DEVOPS, ORG_HEADER: str(org_id)}


async def test_list_only_shows_own_organization(test_client_factory, two_tenants):
    ua, a, run_a, b, run_b = two_tenants
    async with test_client_factory(ua) as client:
        r = await client.post(f"{CR}/query", json=BODY, headers=_cr(a.id))
    assert r.status_code == 200
    assert [row["run_id"] for row in r.json()["rows"]] == [run_a.id]


async def test_other_org_detail_is_not_found(test_client_factory, two_tenants):
    ua, a, run_a, b, run_b = two_tenants
    async with test_client_factory(ua) as client:
        other = await client.get(f"{CR}/{run_b.id}", headers=_cr(a.id))
        missing = await client.get(f"{CR}/2147483000", headers=_cr(a.id))
        own = await client.get(f"{CR}/{run_a.id}", headers=_cr(a.id))
    assert own.status_code == 200
    assert (other.status_code, other.json()) == (missing.status_code, missing.json())
    assert other.status_code == 404


async def test_other_org_audio_is_not_found(test_client_factory, two_tenants):
    ua, a, run_a, b, run_b = two_tenants
    async with test_client_factory(ua) as client:
        other = await client.get(f"{CR}/{run_b.id}/audio", headers=_cr(a.id))
        missing = await client.get(f"{CR}/2147483000/audio", headers=_cr(a.id))
    assert (other.status_code, other.json()) == (missing.status_code, missing.json())
    assert other.status_code == 404


async def test_usage_report_counts_own_organization(test_client_factory, two_tenants):
    ua, a, run_a, b, run_b = two_tenants
    async with test_client_factory(ua) as client:
        r = await client.get(UR, params=USAGE, headers=_ur(a.id))
    assert r.status_code == 200
    assert r.json()["calls"] == 1


# --- 5.5 header contract: secret first, then organization ------------------


@pytest.mark.parametrize(
    "raw", [None, "", "0", "-1", "01", "abc", "1 ", "2147483648", "1;2"]
)
async def test_malformed_or_missing_organization_is_400(
    test_client_factory, two_tenants, raw
):
    ua = two_tenants[0]
    cr_h = {"X-Dograh-Call-Records-Secret": CR_SECRET}
    ur_h = {"X-Dograh-Devops-Secret": DEVOPS}
    if raw is not None:
        cr_h[ORG_HEADER] = raw
        ur_h[ORG_HEADER] = raw
    async with test_client_factory(ua) as client:
        rs = [
            await client.post(f"{CR}/query", json=BODY, headers=cr_h),
            await client.get(f"{CR}/1", headers=cr_h),
            await client.get(f"{CR}/1/audio", headers=cr_h),
            await client.get(UR, params=USAGE, headers=ur_h),
        ]
    assert [(r.status_code, r.json()) for r in rs] == [
        (400, {"detail": "organization_required"})
    ] * 4


async def test_non_tenant_organization_is_403(
    test_client_factory, db_session, two_tenants
):
    ua = two_tenants[0]
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        "scope_org_not_tenant", ua.id
    )
    async with test_client_factory(ua) as client:
        rs = [
            await client.post(f"{CR}/query", json=BODY, headers=_cr(org.id)),
            await client.get(f"{CR}/1", headers=_cr(org.id)),
            await client.get(f"{CR}/1/audio", headers=_cr(org.id)),
            await client.get(UR, params=USAGE, headers=_ur(org.id)),
        ]
    assert [(r.status_code, r.json()) for r in rs] == [
        (403, {"detail": "tenant_unknown"})
    ] * 4


async def test_secret_is_checked_before_organization(
    test_client_factory, two_tenants, monkeypatch
):
    ua = two_tenants[0]
    called = []

    async def spy(organization_id):
        called.append(organization_id)
        return True

    monkeypatch.setattr("api.services.ccp.tenants.is_tenant", spy)
    bad = {"X-Dograh-Call-Records-Secret": "wrong", ORG_HEADER: "nope"}
    bad_ur = {"X-Dograh-Devops-Secret": "wrong", ORG_HEADER: "nope"}
    async with test_client_factory(ua) as client:
        rs = [
            await client.post(f"{CR}/query", json=BODY, headers=bad),
            await client.get(f"{CR}/1", headers=bad),
            await client.get(UR, params=USAGE, headers=bad_ur),
        ]
    assert [r.status_code for r in rs] == [403, 403, 403]
    assert all(
        r.json()["detail"] not in {"organization_required", "tenant_unknown"}
        for r in rs
    )
    assert called == []


async def test_suspended_tenant_is_still_scoped_to_itself(
    test_client_factory, async_session, two_tenants
):
    """Suspension has no call-records effect in this change (tenant-isolation
    〈租戶狀態〉): the header still resolves and scopes to that tenant."""
    from sqlalchemy import update

    ua, a, run_a, b, run_b = two_tenants
    await async_session.execute(
        update(CcpTenantModel)
        .where(CcpTenantModel.organization_id == b.id)
        .values(status="suspended")
    )
    async with test_client_factory(ua) as client:
        r = await client.post(f"{CR}/query", json=BODY, headers=_cr(b.id))
    assert [row["run_id"] for row in r.json()["rows"]] == [run_b.id]
