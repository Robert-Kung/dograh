"""db client lookups require organization_id (mt-s1-foundation 4.11 / 4.12).

tenant-isolation〈歸屬以資料列的組織判定〉: no fallback to ``user_id`` and no
unfiltered query when the organization is missing.
"""

from __future__ import annotations

import pytest

from api.enums import WorkflowRunMode


@pytest.fixture
async def two_orgs(db_session):
    """One creator in orgs A and B, a workflow + run in each."""
    user, _ = await db_session.get_or_create_user_by_provider_id("req_creator")
    org_a, _ = await db_session.get_or_create_organization_by_provider_id(
        "req_org_a", user.id
    )
    org_b, _ = await db_session.get_or_create_organization_by_provider_id(
        "req_org_b", user.id
    )
    out = {"user": user, "a": org_a, "b": org_b}
    for tag, org in (("a", org_a), ("b", org_b)):
        wf = await db_session.create_workflow(
            name=f"req {tag}",
            workflow_definition={"nodes": [], "edges": []},
            user_id=user.id,
            organization_id=org.id,
        )
        run = await db_session.create_workflow_run(
            name=f"req run {tag}",
            workflow_id=wf.id,
            organization_id=wf.organization_id,
            mode=WorkflowRunMode.TEXTCHAT.value,
            user_id=user.id,
        )
        out[f"wf_{tag}"], out[f"run_{tag}"] = wf, run
    return out


def _calls(d):
    """Each db client call with the organization left out (as callers used to)."""
    return {
        "get_workflow": lambda db: db.get_workflow(d["wf_b"].id, user_id=d["user"].id),
        "get_all_workflows": lambda db: db.get_all_workflows(user_id=d["user"].id),
        "update_workflow": lambda db: db.update_workflow(
            d["wf_b"].id,
            name="renamed",
            workflow_definition=None,
            workflow_configurations=None,
            template_context_variables=None,
            user_id=d["user"].id,
        ),
        "get_workflow_name": lambda db: db.get_workflow_name(
            d["wf_b"].id, user_id=d["user"].id
        ),
        "get_all_workflows_for_listing": lambda db: db.get_all_workflows_for_listing(),
        "get_workflow_counts": lambda db: db.get_workflow_counts(),
        "get_workflow_run": lambda db: db.get_workflow_run(d["run_b"].id),
        "get_workflow_runs_by_workflow_id": lambda db: (
            db.get_workflow_runs_by_workflow_id(d["wf_b"].id)
        ),
    }


@pytest.mark.parametrize(
    "name",
    [
        "get_workflow",
        "get_all_workflows",
        "update_workflow",
        "get_workflow_name",
        "get_all_workflows_for_listing",
        "get_workflow_counts",
        "get_workflow_run",
        "get_workflow_runs_by_workflow_id",
    ],
)
async def test_missing_organization_raises(db_session, two_orgs, name):
    with pytest.raises((ValueError, TypeError)):
        await _calls(two_orgs)[name](db_session)


async def test_scoped_lookups_stay_in_org(db_session, two_orgs):
    d = two_orgs
    a, b = d["a"].id, d["b"].id
    assert await db_session.get_workflow(d["wf_b"].id, organization_id=a) is None
    assert (await db_session.get_workflow(d["wf_b"].id, organization_id=b)).id == d[
        "wf_b"
    ].id
    assert await db_session.get_workflow_run(d["run_b"].id, organization_id=a) is None
    runs, total = await db_session.get_workflow_runs_by_workflow_id(
        d["wf_b"].id, organization_id=a
    )
    assert runs == [] and total == 0
    listed = await db_session.get_all_workflows_for_listing(organization_id=a)
    assert {w.id for w in listed} >= {d["wf_a"].id}
    assert d["wf_b"].id not in {w.id for w in listed}
    counts = await db_session.get_workflow_counts(organization_id=a)
    assert counts["total"] == len(listed)
    assert await db_session.get_workflow_name(d["wf_b"].id, organization_id=a) is None
