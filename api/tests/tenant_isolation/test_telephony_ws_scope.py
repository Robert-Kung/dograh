"""Telephony media websocket binds path ids to the run (review gate S5).

The carrier websocket ``/telephony/ws/{workflow_id}/{user_id}/{workflow_run_id}``
has no user session, so the run id is the only anchor.  The path's
workflow_id must be the run's own workflow, the path's user must be the run's
execution user as the server derives it (the campaign's creator for a campaign
run, the workflow's owner otherwise — review round 2 R4), and the telephony
provider is resolved from the run's organization — otherwise an own run could
start another tenant's workflow, use another tenant's credentials, or run as
another member's model settings.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete

from api.db import db_client
from api.db.models import CampaignModel, organization_users_association
from api.enums import WorkflowRunState
from api.routes.telephony import _handle_telephony_websocket


async def _arm_run(async_session, tenant):
    tenant.run.initial_context = {"provider": "twilio"}
    tenant.run.state = WorkflowRunState.INITIALIZED.value
    await async_session.flush()


def _provider():
    return SimpleNamespace(PROVIDER_NAME="twilio", handle_websocket=AsyncMock())


async def _connect(workflow_id, user_id, run_id):
    websocket = AsyncMock()
    provider = _provider()
    lookup = AsyncMock(return_value=provider)
    with patch("api.routes.telephony.get_telephony_provider_for_run", new=lookup):
        await _handle_telephony_websocket(websocket, workflow_id, user_id, run_id)
    return websocket, provider, lookup


async def test_ws_rejects_workflow_of_another_org(two_tenants, async_session):
    a, b = two_tenants.a, two_tenants.b
    await _arm_run(async_session, a)

    websocket, provider, lookup = await _connect(b.workflow.id, a.user.id, a.run.id)

    websocket.close.assert_awaited_once()
    assert websocket.close.await_args.kwargs["code"] == 4404
    assert provider.handle_websocket.await_count == 0
    assert lookup.await_count == 0
    run = await db_client.get_workflow_run_by_id(a.run.id)
    assert run.state == WorkflowRunState.INITIALIZED.value


async def test_ws_rejects_user_outside_run_org(two_tenants, async_session):
    a, b = two_tenants.a, two_tenants.b
    await _arm_run(async_session, a)

    websocket, provider, lookup = await _connect(a.workflow.id, b.user.id, a.run.id)

    websocket.close.assert_awaited_once()
    assert websocket.close.await_args.kwargs["code"] == 4404
    assert provider.handle_websocket.await_count == 0
    assert lookup.await_count == 0
    run = await db_client.get_workflow_run_by_id(a.run.id)
    assert run.state == WorkflowRunState.INITIALIZED.value


async def test_ws_resolves_provider_from_run_org(two_tenants, async_session):
    a = two_tenants.a
    await _arm_run(async_session, a)

    websocket, provider, lookup = await _connect(a.workflow.id, a.user.id, a.run.id)

    websocket.close.assert_not_awaited()
    assert lookup.await_args.args[1] == a.organization_id
    provider.handle_websocket.assert_awaited_once_with(
        websocket, a.workflow.id, a.user.id, a.run.id
    )


async def test_ws_accepts_workflow_owner_no_longer_in_the_org(
    two_tenants, async_session
):
    """review round 2 R4: the workflow already binds the run's organization;
    an owner who left it (or never joined, S9 backfill) must not turn every
    carrier call into a 4404 — dead air on Twilio (C4)."""
    a = two_tenants.a
    await _arm_run(async_session, a)
    await async_session.execute(
        delete(organization_users_association).where(
            organization_users_association.c.user_id == a.user.id
        )
    )
    await async_session.flush()

    websocket, provider, lookup = await _connect(a.workflow.id, a.user.id, a.run.id)

    websocket.close.assert_not_awaited()
    provider.handle_websocket.assert_awaited_once_with(
        websocket, a.workflow.id, a.user.id, a.run.id
    )


async def test_ws_rejects_another_member_of_the_run_org(
    db_session, two_tenants, async_session
):
    """review round 2 R4 (S6): a path user other than the run's execution user
    is refused even inside the run's organization — it would pick whose
    model settings the call runs on."""
    a = two_tenants.a
    await _arm_run(async_session, a)
    other, _ = await db_session.get_or_create_user_by_provider_id("iso_u_a_peer")
    await db_session.add_user_to_organization(other.id, a.organization_id)

    websocket, provider, lookup = await _connect(a.workflow.id, other.id, a.run.id)

    websocket.close.assert_awaited_once()
    assert websocket.close.await_args.kwargs["code"] == 4404
    assert provider.handle_websocket.await_count == 0
    run = await db_client.get_workflow_run_by_id(a.run.id)
    assert run.state == WorkflowRunState.INITIALIZED.value


async def _campaign_run(db_session, async_session, tenant):
    creator, _ = await db_session.get_or_create_user_by_provider_id(
        f"iso_u_{tenant.tag}_campaigner"
    )
    campaign = CampaignModel(
        name="iso campaign",
        organization_id=tenant.organization_id,
        workflow_id=tenant.workflow.id,
        created_by=creator.id,
        source_id="campaigns/x/source.csv",
    )
    async_session.add(campaign)
    await async_session.flush()
    tenant.run.campaign_id = campaign.id
    await _arm_run(async_session, tenant)
    return creator


async def test_ws_campaign_run_runs_as_the_campaign_creator(
    db_session, two_tenants, async_session
):
    """review round 2 R4: a campaign call's execution user is the campaign's
    creator (the dispatcher's webhook URL); the workflow's owner is refused."""
    a = two_tenants.a
    creator = await _campaign_run(db_session, async_session, a)

    websocket, provider, lookup = await _connect(a.workflow.id, a.user.id, a.run.id)
    websocket.close.assert_awaited_once()
    assert websocket.close.await_args.kwargs["code"] == 4404

    websocket, provider, lookup = await _connect(a.workflow.id, creator.id, a.run.id)
    websocket.close.assert_not_awaited()
    provider.handle_websocket.assert_awaited_once_with(
        websocket, a.workflow.id, creator.id, a.run.id
    )
