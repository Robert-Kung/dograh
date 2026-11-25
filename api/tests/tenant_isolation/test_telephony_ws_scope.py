"""Telephony media websocket binds path ids to the run (review gate S5).

The carrier websocket ``/telephony/ws/{workflow_id}/{user_id}/{workflow_run_id}``
has no user session, so the run id is the only anchor.  The path's
workflow_id must be the run's own workflow, the path's user must belong to the
run's organization, and the telephony provider is resolved from the run's
organization — otherwise an own run could start another tenant's workflow or
use another tenant's credentials.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.db import db_client
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
