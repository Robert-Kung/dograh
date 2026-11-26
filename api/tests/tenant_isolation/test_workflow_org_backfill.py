"""workflows / workflow_runs organization_id backfill (mt-s1-foundation 4.3).

tenant-isolation〈工作流與每通通話必屬一組織〉. The schema is already at head
(NOT NULL), so each test relaxes the constraint inside its rolled-back
transaction to recreate the pre-migration shape.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WF_MIG = _load("d4a7c2e9b815_workflows_organization_not_null")
RUN_MIG = _load("e9b5d3f0a726_workflow_runs_organization_id")


async def _exec(async_session, sql: str, **params):
    return await async_session.execute(text(sql), params)


async def _org(async_session, tag: str) -> int:
    return (
        await _exec(
            async_session,
            "INSERT INTO organizations (provider_id, created_at) "
            "VALUES (:p, now()) RETURNING id",
            p=f"bf_org_{tag}",
        )
    ).scalar_one()


async def _user(async_session, tag: str, orgs: list[int]) -> int:
    uid = (
        await _exec(
            async_session,
            "INSERT INTO users (provider_id, created_at) VALUES (:p, now()) RETURNING id",
            p=f"bf_user_{tag}",
        )
    ).scalar_one()
    for o in orgs:
        await _exec(
            async_session,
            "INSERT INTO organization_users (user_id, organization_id) VALUES (:u, :o)",
            u=uid,
            o=o,
        )
    return uid


async def _null_org_workflow(db_session, async_session, user_id, org_for_create):
    wf = await db_session.create_workflow(
        name="bf",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user_id,
        organization_id=org_for_create,
    )
    await _exec(
        async_session,
        "UPDATE workflows SET organization_id = NULL WHERE id = :w",
        w=wf.id,
    )
    return wf.id


async def _phone(async_session, org: int, wf_id: int, address: str):
    cfg = (
        await _exec(
            async_session,
            "INSERT INTO telephony_configurations (organization_id, name, provider, "
            "credentials, is_default_outbound, created_at, updated_at) "
            "VALUES (:o, :n, 'livekit', '{}', false, now(), now()) RETURNING id",
            o=org,
            n=f"bf {address}",
        )
    ).scalar_one()
    await _exec(
        async_session,
        "INSERT INTO telephony_phone_numbers (organization_id, "
        "telephony_configuration_id, address, address_normalized, address_type, "
        "inbound_workflow_id, is_active, created_at, updated_at) "
        "VALUES (:o, :c, :a, :a, 'pstn', :w, true, now(), now())",
        o=org,
        c=cfg,
        a=address,
        w=wf_id,
    )


@pytest.fixture
async def pre_migration(async_session):
    await _exec(
        async_session,
        "ALTER TABLE workflows ALTER COLUMN organization_id DROP NOT NULL",
    )
    # Only this test's NULL rows are in scope; existing rows already have orgs.
    yield


async def _backfill(async_session):
    conn = await async_session.connection()
    return await conn.run_sync(WF_MIG.backfill_workflow_organizations)


async def _wf_org(async_session, wf_id):
    return (
        await _exec(
            async_session,
            "SELECT organization_id FROM workflows WHERE id = :w",
            w=wf_id,
        )
    ).scalar_one()


@pytest.mark.parametrize(
    "creator_in",
    ["both", "none"],
    ids=["creator_in_two_orgs_incl_phone_org", "creator_in_no_org"],
)
async def test_backfill_from_phone_number_org(
    pre_migration, db_session, async_session, capsys, creator_in
):
    """The number's organization wins when the creator is ambiguous (two
    organizations, the number's among them) or belongs to none (S9 allows
    it) — review round 2 R17."""
    a, b = (
        await _org(async_session, f"p_a_{creator_in}"),
        await _org(async_session, f"p_b_{creator_in}"),
    )
    uid = await _user(
        async_session, f"p_{creator_in}", [a, b] if creator_in == "both" else []
    )
    wf = await _null_org_workflow(db_session, async_session, uid, a)
    await _phone(async_session, b, wf, "+886299990001")

    out = await _backfill(async_session)

    assert await _wf_org(async_session, wf) == b
    assert out["rows"] >= 1
    assert "backfilled" in capsys.readouterr().err


async def test_backfill_from_creator_single_org(
    pre_migration, db_session, async_session
):
    a = await _org(async_session, "s_a")
    uid = await _user(async_session, "s", [a])
    wf = await _null_org_workflow(db_session, async_session, uid, a)

    await _backfill(async_session)

    assert await _wf_org(async_session, wf) == a


@pytest.mark.scenario("tenant-isolation:無法判定歸屬的工作流")
@pytest.mark.parametrize(
    "shape",
    ["no_candidate", "contradiction", "phone_outside_creator_orgs", "two_phone_orgs"],
)
async def test_backfill_aborts_when_undeterminable(
    pre_migration, db_session, async_session, shape
):
    a, b = (
        await _org(async_session, f"x_a_{shape}"),
        await _org(async_session, f"x_b_{shape}"),
    )
    if shape == "no_candidate":
        uid = await _user(async_session, shape, [a, b])
        wf = await _null_org_workflow(db_session, async_session, uid, a)
    elif shape == "contradiction":
        uid = await _user(async_session, shape, [a])
        wf = await _null_org_workflow(db_session, async_session, uid, a)
        await _phone(async_session, b, wf, "+886299990002")
    elif shape == "phone_outside_creator_orgs":
        # Creator in two orgs, the number's org is neither (review gate S9).
        c = await _org(async_session, f"x_c_{shape}")
        uid = await _user(async_session, shape, [a, b])
        wf = await _null_org_workflow(db_session, async_session, uid, a)
        await _phone(async_session, c, wf, "+886299990005")
    else:
        uid = await _user(async_session, shape, [a])
        wf = await _null_org_workflow(db_session, async_session, uid, a)
        await _phone(async_session, a, wf, "+886299990003")
        await _phone(async_session, b, wf, "+886299990004")
    ok = await _null_org_workflow(
        db_session, async_session, await _user(async_session, f"ok_{shape}", [a]), a
    )

    with pytest.raises(WF_MIG.WorkflowBackfillAborted) as exc:
        await _backfill(async_session)

    assert f"[{wf}]" in str(exc.value)
    # Nothing written, not even the resolvable workflow.
    assert await _wf_org(async_session, ok) is None
    assert await _wf_org(async_session, wf) is None


@pytest.mark.scenario("tenant-isolation:無法判定歸屬的工作流")
async def test_backfill_never_uses_selected_organization(
    pre_migration, db_session, async_session
):
    a, b = await _org(async_session, "sel_a"), await _org(async_session, "sel_b")
    uid = await _user(async_session, "sel", [a, b])
    await _exec(
        async_session,
        "UPDATE users SET selected_organization_id = :o WHERE id = :u",
        o=a,
        u=uid,
    )
    wf = await _null_org_workflow(db_session, async_session, uid, a)

    with pytest.raises(WF_MIG.WorkflowBackfillAborted):
        await _backfill(async_session)
    assert await _wf_org(async_session, wf) is None


async def test_run_backfill_takes_workflow_org(db_session, async_session):
    a = await _org(async_session, "run_a")
    uid = await _user(async_session, "run", [a])
    wf = await db_session.create_workflow(
        name="bf run",
        workflow_definition={"nodes": [], "edges": []},
        user_id=uid,
        organization_id=a,
    )
    await _exec(
        async_session,
        "ALTER TABLE workflow_runs ALTER COLUMN organization_id DROP NOT NULL",
    )
    run_id = (
        await _exec(
            async_session,
            "INSERT INTO workflow_runs (name, workflow_id, mode, call_type, state, "
            "storage_backend, created_at, usage_info, cost_info, initial_context, "
            "gathered_context, logs, extra, annotations) VALUES ('r', :w, 'livekit', "
            "'inbound', 'initialized', 's3', now(), '{}', '{}', '{}', '{}', '{}', "
            "'{}', '{}') RETURNING id",
            w=wf.id,
        )
    ).scalar_one()

    conn = await async_session.connection()
    n = await conn.run_sync(RUN_MIG.backfill_run_organizations)

    assert n >= 1
    got = (
        await _exec(
            async_session,
            "SELECT organization_id FROM workflow_runs WHERE id = :r",
            r=run_id,
        )
    ).scalar_one()
    assert got == a
