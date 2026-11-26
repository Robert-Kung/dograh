"""Two-tenant fixture for the cross-tenant isolation suite (mt-s1-foundation 8.1).

tenant-isolation〈跨租戶隔離測試〉: one DB, two CCP tenants, each with its own
organization, ``ccp_tenants`` row, member user (selected organization set),
workflow, telephony configuration + DID, one finished livekit inbound run and
its ``ccp_call_meta`` row. Runs sit in March 2021 so date-ranged reports see
nothing else.

Every test in this package is tagged with the spec scenario(s) it covers via
``@pytest.mark.scenario("<scenario name>")``; the parent repo's
``scripts/check_isolation_scenarios.py`` checks every scenario has one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from starlette.routing import Route, WebSocketRoute

from api.db.models import (
    CcpCallMetaModel,
    CcpTenantModel,
    OrganizationModel,
    TelephonyPhoneNumberModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)

RUN_AT = datetime(2021, 3, 10, 2, tzinfo=UTC)
DIDS = {"a": "+886221110001", "b": "+886221110002"}


@dataclass
class Tenant:
    tag: str
    org: OrganizationModel
    user: UserModel
    workflow: WorkflowModel
    number: TelephonyPhoneNumberModel
    run: WorkflowRunModel

    @property
    def organization_id(self) -> int:
        return self.org.id


@dataclass
class TwoTenants:
    a: Tenant
    b: Tenant


async def make_tenant(db_session, async_session, tag: str, *, status="active"):
    user, _ = await db_session.get_or_create_user_by_provider_id(f"iso_u_{tag}")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"iso_org_{tag}", user.id
    )
    await db_session.add_user_to_organization(user.id, org.id)
    await db_session.update_user_selected_organization(user.id, org.id)
    async_session.add(
        CcpTenantModel(organization_id=org.id, display_name=f"iso {tag}", status=status)
    )
    await async_session.flush()
    user = await db_session.get_user_by_id(user.id)
    wf = await db_session.create_workflow(
        name=f"iso {tag}",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org.id,
    )
    cfg = await db_session.create_telephony_configuration(
        organization_id=org.id,
        name=f"iso cfg {tag}",
        provider="livekit",
        credentials={},
    )
    number = await db_session.create_phone_number(
        organization_id=org.id,
        telephony_configuration_id=cfg.id,
        address=DIDS[tag],
        inbound_workflow_id=wf.id,
    )
    run = WorkflowRunModel(
        name=f"iso run {tag}",
        workflow_id=wf.id,
        organization_id=org.id,
        mode="livekit",
        call_type="inbound",
        is_completed=True,
        created_at=RUN_AT,
        annotations={"call_outcome": "ai_completed"},
        usage_info={"call_duration_seconds": 42},
        initial_context={"did": DIDS[tag]},
        storage_backend="minio",
    )
    async_session.add(run)
    await async_session.flush()
    async_session.add(
        CcpCallMetaModel(
            workflow_run_id=run.id,
            caller_masked="09****",
            caller_last4="0000",
            audio_started_at=RUN_AT,
        )
    )
    await async_session.flush()
    return Tenant(tag, org, user, wf, number, run)


@pytest.fixture
async def two_tenants(db_session, async_session) -> TwoTenants:
    return TwoTenants(
        a=await make_tenant(db_session, async_session, "a"),
        b=await make_tenant(db_session, async_session, "b"),
    )


# --- route hit log (mt-s1-foundation 8.3) ------------------------------------
# The route inventory runs the cited (a) tests in a subprocess with
# ROUTE_HITS_FILE set and checks each test hit the route it is cited for.
# Recorded at the matched route (``self.path`` is the route template, the same
# value as ``request.scope["route"].path``), so every client — httpx
# ASGITransport, TestClient, websockets — is covered without wrapping the app.
ROUTE_HITS_ENV = "ROUTE_HITS_FILE"


@pytest.fixture(autouse=True)
def _record_route_hits(request, monkeypatch):
    out = os.environ.get(ROUTE_HITS_ENV)
    if not out:
        yield
        return
    hits: set[str] = set()
    http_handle, ws_handle = Route.handle, WebSocketRoute.handle

    async def record_http(self, scope, receive, send):
        hits.add(f"{scope['method']} {self.path}")
        await http_handle(self, scope, receive, send)

    async def record_ws(self, scope, receive, send):
        hits.add(f"WS {self.path}")
        await ws_handle(self, scope, receive, send)

    monkeypatch.setattr(Route, "handle", record_http)
    monkeypatch.setattr(WebSocketRoute, "handle", record_ws)
    yield
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps({"node": request.node.nodeid, "hits": sorted(hits)}) + "\n")
