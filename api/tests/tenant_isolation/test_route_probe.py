"""Cross-org route probe (mt-s1-foundation 8.3a) — tenant-isolation〈跨租戶隔離測試〉(c).

Every ``PROBED`` route runs its case from ``probe_cases.py`` as tenant B
against a world where A and B each hold one of every resource. A case passes
only if it hit its route template, made the check its kind requires, and left
every row A owns unchanged. The case table is checked against ``PROBED`` in
both directions; each rule has a synthetic red light.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from starlette.routing import Route, WebSocketRoute

from api.tests.tenant_isolation import route_scope
from api.tests.tenant_isolation.probe_cases import (
    CASES,
    FOREIGN,
    OWN,
    PROMOTED,
    CaseSpec,
    Probe,
    check_probe_cases,
    check_probe_run,
    check_promoted,
)
from api.tests.tenant_isolation.probe_world import (
    FakeEmbeddingService,
    FakeMps,
    FakeProvider,
    FakeStorage,
    SignalingCalls,
    build_world,
    snapshot_org,
)

SCENARIO = "tenant-isolation:探針抓到傳了組織卻不擋的路由"
PROMOTED_NODE = "tests/tenant_isolation/test_route_probe.py::test_promoted_route_probe"


@pytest.fixture
async def world(db_session, async_session, two_tenants):
    return await build_world(db_session, async_session, two_tenants)


@pytest.fixture
def fakes(monkeypatch, world, db_session):
    """Replace external systems; keep every organization check real."""
    import api.routes.campaign as campaign_routes
    import api.routes.knowledge_base as kb_routes
    import api.routes.organization as org_routes
    import api.routes.organization_usage as usage_routes
    import api.routes.s3_signed_url as s3_routes
    import api.routes.service_keys as service_key_routes
    import api.routes.telephony as telephony_routes
    import api.routes.turn_credentials as turn_routes
    import api.routes.user as user_routes
    import api.routes.webrtc_signaling as signaling_routes
    import api.routes.workflow as workflow_routes
    import api.routes.workflow_recording as recording_routes
    import api.services.gen_ai as gen_ai
    from api.services.campaign.sources.csv import CSVSyncService

    storage = FakeStorage()
    for mod in (
        workflow_routes,
        kb_routes,
        recording_routes,
        s3_routes,
        campaign_routes,
    ):
        monkeypatch.setattr(mod, "storage_fs", storage)
    monkeypatch.setattr(s3_routes, "get_storage_for_backend", lambda backend: storage)

    mps = FakeMps(world)
    for mod in (
        service_key_routes,
        user_routes,
        workflow_routes,
        usage_routes,
        recording_routes,
    ):
        monkeypatch.setattr(mod, "mps_service_key_client", mps)

    csv_rows = {
        t.csv_key: [["phone_number", "name"], ["+886900001234", t.token]]
        for t in (world.a, world.b)
    }

    async def fetch_csv(self, key):
        if key not in csv_rows:
            raise ValueError(f"Failed to access CSV file: {key}")
        return csv_rows[key]

    monkeypatch.setattr(CSVSyncService, "_fetch_csv_data", fetch_csv)

    jobs = []

    async def enqueue(*args, **kw):
        jobs.append(args)

    monkeypatch.setattr(kb_routes, "enqueue_job", enqueue)

    class _Sync:
        async def broadcast(self, *a, **kw):
            return None

    monkeypatch.setattr(org_routes, "get_worker_sync_manager", lambda: _Sync())

    async def no_validate(self, *a, **kw):
        return {"status": []}

    monkeypatch.setattr(org_routes.UserConfigurationValidator, "validate", no_validate)

    async def embedding_service(**kw):
        return FakeEmbeddingService(db_session)

    monkeypatch.setattr(gen_ai, "build_embedding_service", embedding_service)

    async def default_provider(organization_id):
        return FakeProvider()

    monkeypatch.setattr(
        telephony_routes, "get_default_telephony_provider", default_provider
    )
    monkeypatch.setattr(turn_routes, "TURN_SECRET", "probe-turn-secret")

    calls = SignalingCalls()

    async def handle_websocket(websocket, workflow_id, workflow_run_id, user):
        calls.calls.append((workflow_id, workflow_run_id, user.id))
        await websocket.accept()
        await websocket.close()

    monkeypatch.setattr(
        signaling_routes.signaling_manager, "handle_websocket", handle_websocket
    )
    return {"signaling": calls, "jobs": jobs}


@asynccontextmanager
async def as_user(user):
    from httpx import ASGITransport, AsyncClient

    from api.app import app
    from api.services.auth.depends import get_user, get_user_ws

    async def _user():
        return user

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_user] = _user
    app.dependency_overrides[get_user_ws] = _user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


async def ws_connect(path: str) -> dict:
    """Drive one websocket handshake in-process (same event loop as the DB)."""
    from api.app import app

    inbox: asyncio.Queue = asyncio.Queue()
    sent: list[dict] = []
    await inbox.put({"type": "websocket.connect"})

    async def receive():
        return await inbox.get()

    async def send(message):
        sent.append(message)
        if message["type"] in ("websocket.accept", "websocket.close"):
            await inbox.put({"type": "websocket.disconnect", "code": 1000})

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "http_version": "1.1",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "subprotocols": [],
        "state": {},
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=10)
    except Exception as e:  # noqa: BLE001 - the handshake outcome is what we assert
        sent.append({"type": "exception", "repr": repr(e)})
    return {
        "accepted": any(m["type"] == "websocket.accept" for m in sent),
        "messages": sent,
    }


@pytest.fixture
def route_hits(monkeypatch) -> set[str]:
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
    return hits


async def _run_probe(route, spec, world, fakes, route_hits, async_session):
    before = await snapshot_org(async_session, world.a.org_id)
    async with as_user(world.b.user) as client:
        p = Probe(
            client=client,
            w=world,
            ws=ws_connect,
            extras={"route": route, "signaling": fakes["signaling"]},
        )
        await spec.fn(p)
    problems = check_probe_run(route, spec, route_hits, p.checks)
    assert not problems, "\n".join(problems)
    after = await snapshot_org(async_session, world.a.org_id)
    changed = {
        k: (before[k], after.get(k)) for k in before if before[k] != after.get(k)
    }
    assert not changed, f"B's request changed A's rows: {changed}"


@pytest.mark.scenario(SCENARIO)
@pytest.mark.parametrize("route", route_scope.PROBED)
async def test_cross_org_probe(route, world, fakes, route_hits, async_session):
    await _run_probe(route, CASES[route], world, fakes, route_hits, async_session)


@pytest.mark.scenario(SCENARIO)
@pytest.mark.parametrize("route", sorted(PROMOTED))
async def test_promoted_route_probe(route, world, fakes, route_hits, async_session):
    """Probes that failed before their fix; the routes now sit in (a)."""
    await _run_probe(route, PROMOTED[route], world, fakes, route_hits, async_session)


@pytest.mark.scenario(SCENARIO)
def test_every_probed_route_has_exactly_one_case():
    problems = check_probe_cases(CASES, route_scope.PROBED) + check_promoted(
        PROMOTED, route_scope.ORG_SCOPED, PROMOTED_NODE
    )
    assert not problems, "\n".join(problems)


# --- red lights ----------------------------------------------------------------

ROUTE = "GET /api/v1/x/{id}"


async def _noop(p):
    return None


def test_red_probed_route_without_case_and_stray_case():
    spec = CaseSpec(FOREIGN, _noop)
    assert check_probe_cases({}, [ROUTE]) == [f"{ROUTE}: (c) route has no probe case"]
    assert check_probe_cases({ROUTE: spec}, []) == [
        f"{ROUTE}: probe case for a route not in (c)"
    ]
    assert check_probe_cases({ROUTE: CaseSpec("maybe", _noop)}, [ROUTE]) == [
        f"{ROUTE}: unknown probe kind 'maybe'"
    ]
    assert check_probe_cases({ROUTE: spec}, [ROUTE]) == []


async def test_red_snapshot_sees_writes_to_a(world, db_session, async_session):
    """The "A's rows unchanged" rule fires on an edit and on a B row aimed at A."""
    from sqlalchemy import text

    before = await snapshot_org(async_session, world.a.org_id)
    await async_session.execute(
        text("UPDATE external_credentials SET name = 'x' WHERE id = :i"),
        {"i": world.a.credential.id},
    )
    await db_session.create_campaign(
        name="B on A's workflow",
        workflow_id=world.a.workflow.id,
        source_type="csv",
        source_id=world.b.csv_key,
        user_id=world.b.user.id,
        organization_id=world.b.org_id,
    )
    after = await snapshot_org(async_session, world.a.org_id)
    assert before["external_credentials"] != after["external_credentials"]
    assert before["campaigns<-workflow"] != after["campaigns<-workflow"]


def test_red_promoted_case_not_cited_in_org_scope():
    node = "tests/tenant_isolation/t.py::t"
    assert check_promoted([ROUTE], {}, node) == [
        f"{ROUTE}: promoted probe case but (a) does not cite {node}"
    ]
    assert check_promoted([ROUTE], {ROUTE: ("x",)}, node) != []
    assert check_promoted([ROUTE], {ROUTE: (node,)}, node) == []


def test_red_case_that_misses_its_route_or_check():
    spec = CaseSpec(FOREIGN, _noop)
    missed = check_probe_run(ROUTE, spec, {"GET /api/v1/y"}, [FOREIGN])
    assert len(missed) == 1 and missed[0].startswith(f"{ROUTE}: probe never hit")
    assert check_probe_run(ROUTE, spec, {ROUTE}, [OWN]) == [
        f"{ROUTE}: foreign probe made no foreign check"
    ]
    assert check_probe_run(ROUTE, spec, {ROUTE}, [FOREIGN]) == []


class _Resp:
    class request:  # noqa: N801 - mimics httpx.Response.request
        method = "GET"
        url = "http://test/x"

    def __init__(self, status, text):
        self.status_code, self.text = status, text


def _probe():
    class _T:
        markers = ["TOKEN-A", "uuid-a"]

    class _W:
        a = _T()

    return Probe(client=None, w=_W(), ws=None)


@pytest.mark.parametrize(
    "status,body,kw",
    [
        (200, '{"name": "TOKEN-A wf"}', {}),  # A's identifier in a 200
        (400, "no uuid-a here?", {}),  # ...also in an error body
        (422, "{}", {}),  # body rejected: not a valid write probe
        (401, "{}", {}),  # auth not applied
        (500, "{}", {}),  # server error without a stated reason
    ],
)
def test_red_foreign_verdict(status, body, kw):
    with pytest.raises(AssertionError):
        _probe().foreign(_Resp(status, body), **kw)


@pytest.mark.parametrize(
    "status,body,kw",
    [
        (404, "TOKEN-A not found", {}),  # 403/404 pass even when echoing
        (403, "", {}),
        (200, '{"items": []}', {}),
        (500, "{}", {"server_error_ok": "reason"}),
    ],
)
def test_green_foreign_verdict(status, body, kw):
    _probe().foreign(_Resp(status, body), **kw)


def test_red_own_verdict():
    p = _probe()
    with pytest.raises(AssertionError):
        p.own(_Resp(200, "TOKEN-A"))
    with pytest.raises(AssertionError):
        p.own(_Resp(200, "{}"), "TOKEN-B")  # control: own data missing
    with pytest.raises(AssertionError):
        p.own(_Resp(500, "{}"))
    p.own(_Resp(200, "TOKEN-B"), "TOKEN-B")
