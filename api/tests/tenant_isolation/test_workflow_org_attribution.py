"""Attribution by the row's organization, not the creator's selected org
(mt-s1-foundation 4.1 / 4.9).

tenant-isolation〈歸屬以資料列的組織判定〉.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from api.db.models import WorkflowRunModel
from api.enums import WorkflowRunMode


async def _creator_in_two_orgs(db_session):
    user, _ = await db_session.get_or_create_user_by_provider_id("attr_creator")
    org_a, _ = await db_session.get_or_create_organization_by_provider_id(
        "attr_org_a", user.id
    )
    org_b, _ = await db_session.get_or_create_organization_by_provider_id(
        "attr_org_b", user.id
    )
    await db_session.add_user_to_organization(user.id, org_a.id)
    await db_session.add_user_to_organization(user.id, org_b.id)
    await db_session.update_user_selected_organization(user.id, org_a.id)
    wf = await db_session.create_workflow(
        name="AI A",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org_a.id,
    )
    run = await db_session.create_workflow_run(
        name="run A",
        workflow_id=wf.id,
        mode=WorkflowRunMode.LIVEKIT.value,
        user_id=user.id,
        organization_id=org_a.id,
    )
    return user, org_a, org_b, wf, run


async def _complete_with_usage(async_session, run_id: int):
    await async_session.execute(
        update(WorkflowRunModel)
        .where(WorkflowRunModel.id == run_id)
        .values(
            is_completed=True,
            usage_info={"call_duration_seconds": 30},
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    await async_session.flush()


async def _switched(db_session, async_session):
    user, org_a, org_b, wf, run = await _creator_in_two_orgs(db_session)
    await _complete_with_usage(async_session, run.id)
    # The creator now looks at org B. Nothing about the workflow or run moved.
    await db_session.update_user_selected_organization(user.id, org_b.id)
    return org_a, org_b, wf, run


@pytest.mark.scenario("tenant-isolation:建立者切換組織後歸屬不變")
async def test_workflows_by_ids_follow_workflow_org(db_session, async_session):
    org_a, org_b, wf, _ = await _switched(db_session, async_session)
    got_a = await db_session.get_workflows_by_ids([wf.id], org_a.id)
    assert [w.id for w in got_a] == [wf.id]
    assert await db_session.get_workflows_by_ids([wf.id], org_b.id) == []


@pytest.mark.scenario("tenant-isolation:建立者切換組織後歸屬不變")
async def test_usage_history_and_export_follow_run_org(db_session, async_session):
    org_a, org_b, _, run = await _switched(db_session, async_session)
    runs_a, total_a, *_ = await db_session.get_usage_history(org_a.id)
    runs_b, total_b, *_ = await db_session.get_usage_history(org_b.id)
    assert [r["id"] for r in runs_a] == [run.id] and total_a == 1
    assert runs_b == [] and total_b == 0
    report_a = await db_session.get_usage_runs_for_report(org_a.id)
    assert [r.id for r in report_a] == [run.id]
    assert await db_session.get_usage_runs_for_report(org_b.id) == []


@pytest.mark.scenario("tenant-isolation:建立者切換組織後歸屬不變")
async def test_daily_usage_follows_run_org(db_session, async_session):
    org_a, org_b, _, _ = await _switched(db_session, async_session)
    now = datetime.now(UTC)
    window = (now - timedelta(days=1), now + timedelta(days=1))
    daily_a = await db_session.get_daily_usage_breakdown(org_a.id, *window, 0.0)
    daily_b = await db_session.get_daily_usage_breakdown(org_b.id, *window, 0.0)
    assert sum(d["call_count"] for d in daily_a["breakdown"]) == 1
    assert sum(d["call_count"] for d in daily_b["breakdown"]) == 0


@pytest.mark.scenario("tenant-isolation:建立者切換組織後歸屬不變")
async def test_run_context_org_is_run_org(db_session, async_session):
    org_a, _, _, run = await _switched(db_session, async_session)
    _, ctx_org = await db_session.get_workflow_run_with_context(run.id)
    assert ctx_org == org_a.id


# --- 4.7 every new run carries its workflow's organization -------------------


@pytest.mark.scenario("tenant-isolation:新 run 帶組織")
@pytest.mark.parametrize(
    "mode, by_user",
    [
        (WorkflowRunMode.LIVEKIT.value, False),  # dispatcher: org only, no user
        (WorkflowRunMode.TEXTCHAT.value, True),  # text-chat route
        (WorkflowRunMode.TWILIO.value, True),  # API trigger (public_agent)
    ],
)
async def test_new_run_org_equals_workflow_org(db_session, mode, by_user):
    user, org_a, org_b, wf, _ = await _creator_in_two_orgs(db_session)
    await db_session.update_user_selected_organization(user.id, org_b.id)

    run = await db_session.create_workflow_run(
        name=f"run {mode}",
        workflow_id=wf.id,
        mode=mode,
        user_id=user.id if by_user else None,
        organization_id=org_a.id,
    )

    assert run.organization_id == wf.organization_id == org_a.id


async def test_provisioned_workflow_without_creator_gets_run(db_session):
    _, org_a, _, _, _ = await _creator_in_two_orgs(db_session)
    wf = await db_session.create_workflow(
        name="provisioned",
        workflow_definition={"nodes": [], "edges": []},
        user_id=None,
        organization_id=org_a.id,
    )
    run = await db_session.create_workflow_run(
        name="run",
        workflow_id=wf.id,
        mode=WorkflowRunMode.LIVEKIT.value,
        user_id=None,
        organization_id=org_a.id,
    )
    assert run.organization_id == org_a.id


@pytest.mark.scenario("tenant-isolation:新 run 帶組織")
async def test_livekit_org_mismatch_creates_no_run(db_session, async_session):
    _, _, org_b, wf, _ = await _creator_in_two_orgs(db_session)
    before = (
        await async_session.execute(select(func.count()).select_from(WorkflowRunModel))
    ).scalar_one()

    with pytest.raises(ValueError):
        await db_session.create_workflow_run(
            name="mismatch",
            workflow_id=wf.id,
            mode=WorkflowRunMode.LIVEKIT.value,
            user_id=None,
            organization_id=org_b.id,
        )

    after = (
        await async_session.execute(select(func.count()).select_from(WorkflowRunModel))
    ).scalar_one()
    assert after == before


async def test_create_workflow_requires_organization(db_session):
    with pytest.raises(ValueError):
        await db_session.create_workflow(
            name="orphan",
            workflow_definition={"nodes": [], "edges": []},
            user_id=None,
            organization_id=None,
        )


# --- 4.9 user_id NULL workflows still count; superuser list shows run org ----


async def _provisioned_run(db_session, async_session):
    """A workflow with no creator (change 3 provisioning) and one finished call."""
    _, org_a, org_b, _, _ = await _creator_in_two_orgs(db_session)
    wf = await db_session.create_workflow(
        name="provisioned",
        workflow_definition={"nodes": [], "edges": []},
        user_id=None,
        organization_id=org_b.id,
    )
    run = await db_session.create_workflow_run(
        name="run",
        workflow_id=wf.id,
        mode=WorkflowRunMode.LIVEKIT.value,
        user_id=None,
        organization_id=org_b.id,
    )
    await _complete_with_usage(async_session, run.id)
    return org_b, wf, run


@pytest.mark.scenario("tenant-isolation:無建立者的工作流仍計入用量")
async def test_creatorless_workflow_counts_in_usage_and_batch(
    db_session, async_session
):
    org, wf, run = await _provisioned_run(db_session, async_session)

    assert [w.id for w in await db_session.get_workflows_by_ids([wf.id], org.id)] == [
        wf.id
    ]
    runs, total, *_ = await db_session.get_usage_history(org.id)
    assert [r["id"] for r in runs] == [run.id] and total == 1
    assert [r.id for r in await db_session.get_usage_runs_for_report(org.id)] == [
        run.id
    ]
    now = datetime.now(UTC)
    daily = await db_session.get_daily_usage_breakdown(
        org.id, now - timedelta(days=1), now + timedelta(days=1), 0.0
    )
    assert sum(d["call_count"] for d in daily["breakdown"]) == 1


async def test_superadmin_list_shows_run_org(db_session, async_session):
    org_a, org_b, _, run = await _switched(db_session, async_session)
    _, _, prov = await _provisioned_run(db_session, async_session)

    rows, _ = await db_session.get_workflow_runs_for_superadmin(
        limit=50,
        filters=[{"attribute": "runId", "type": "number", "value": {"value": run.id}}],
    )
    assert [(r["id"], r["organization_id"]) for r in rows] == [(run.id, org_a.id)]

    rows, _ = await db_session.get_workflow_runs_for_superadmin(
        limit=50,
        filters=[{"attribute": "runId", "type": "number", "value": {"value": prov.id}}],
    )
    assert [(r["id"], r["organization_id"]) for r in rows] == [(prov.id, org_b.id)]
