"""Cross-tenant checks driven by the shared two-tenant fixture (8.1 / 8.2).

The fixture builds tenants A and B side by side; these tests exercise the real
DID resolver and run creation on that state.
"""

from __future__ import annotations

import asyncio

import pytest

from api.db import db_client
from api.routes.livekit import _did_resolver
from api.services.ccp import tenants
from api.services.pipecat import livekit_dispatcher, run_pipeline
from api.tests.tenant_isolation.conftest import DIDS


async def test_fixture_builds_two_disjoint_tenants(two_tenants):
    a, b = two_tenants.a, two_tenants.b
    assert a.organization_id != b.organization_id
    for t in (a, b):
        assert await tenants.is_tenant(t.organization_id)
        assert t.workflow.organization_id == t.organization_id
        assert t.number.organization_id == t.organization_id
        assert t.run.organization_id == t.organization_id
        assert t.user.selected_organization_id == t.organization_id


@pytest.mark.scenario("livekit-dispatch:已設定 DID")
@pytest.mark.scenario("tenant-isolation:解析回傳號碼列的組織")
async def test_each_did_resolves_to_its_own_tenant(two_tenants):
    for t in (two_tenants.a, two_tenants.b):
        resolved = await _did_resolver(DIDS[t.tag])
        assert resolved.mapped
        assert (resolved.workflow_id, resolved.organization_id) == (
            t.workflow.id,
            t.organization_id,
        )
        assert resolved.tenant_status == "active"


@pytest.mark.scenario("livekit-dispatch:run 帶組織")
@pytest.mark.scenario("livekit-dispatch:簽發 token")
@pytest.mark.scenario("tenant-isolation:新 run 帶組織")
async def test_second_tenant_call_creates_run_in_second_tenant(
    two_tenants, monkeypatch
):
    piped = {}

    async def fake_pipeline(**kw):
        piped.update(kw)

    async def no_fallback(room, reason, workflow_run_id=None):
        raise AssertionError(f"fallback must not fire: {reason}")

    monkeypatch.setattr(run_pipeline, "run_pipeline_livekit", fake_pipeline)
    monkeypatch.setattr(
        livekit_dispatcher, "_sign_agent_token", lambda room, ident: f"jwt:{ident}"
    )
    monkeypatch.setenv("LIVEKIT_URL", "ws://test")
    monkeypatch.setenv("LIVEKIT_MAX_CONCURRENT_CALLS", "0")

    b = two_tenants.b
    launched = await livekit_dispatcher._dispatch(
        "cs-iso-b",
        {"sip.trunkPhoneNumber": DIDS["b"]},
        _did_resolver,
        no_fallback,
        "ws://test",
    )
    for _ in range(5):  # let the pipeline task start
        await asyncio.sleep(0)

    assert launched is True
    run = await db_client.get_workflow_run_by_id(piped["workflow_run_id"])
    assert run.organization_id == b.organization_id
    assert run.workflow_id == b.workflow.id
    assert piped["organization_id"] == b.organization_id
    assert piped["token"] == f"jwt:agent-{run.id}"
    assert piped["room_name"] == "cs-iso-b"
