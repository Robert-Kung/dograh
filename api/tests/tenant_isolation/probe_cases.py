"""Cross-org probe cases, one per (c) route (mt-s1-foundation 8.3a).

tenant-isolation〈跨租戶隔離測試〉(c): every ``PROBED`` route has a case here.
A case runs as tenant B against the two-tenant world (``probe_world.py``):

- ``FOREIGN`` routes take a resource identifier: the case sends A's existing
  identifier (path, query or body) and calls ``p.foreign(resp)`` — 403/404,
  or a response that holds none of A's identifiers; 401, 422 and 5xx fail
  (a 5xx passes only with a stated reason). Writes are sent with a valid body.
- ``OWN`` routes take none (create/list in the caller's org): the case calls
  ``p.own(resp)`` with A and B both holding data — success and none of A's
  identifiers (optionally B's own token as the positive control).

The test (``test_route_probe.py``) additionally checks that the case hit its
route template and that none of A's rows changed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

FOREIGN = "foreign"
OWN = "own"

V1 = "/api/v1"


@dataclass
class Probe:
    client: object
    w: object  # ProbeWorld
    ws: Callable[..., Awaitable[dict]]
    extras: dict = field(default_factory=dict)
    checks: list[str] = field(default_factory=list)

    @property
    def a(self):
        return self.w.a

    @property
    def b(self):
        return self.w.b

    def leaked(self, body: str) -> list[str]:
        return [m for m in self.a.markers if m and m in body]

    def foreign(self, r, *, server_error_ok: str | None = None):
        self.checks.append(FOREIGN)
        desc = f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:300]}"
        assert r.status_code != 401, f"auth not applied: {desc}"
        assert r.status_code != 422, f"body/query rejected, probe not valid: {desc}"
        if r.status_code >= 500:
            assert server_error_ok, f"server error: {desc}"
        if r.status_code in (403, 404):
            return r
        leaked = self.leaked(r.text)
        assert not leaked, f"A's identifiers {leaked} in response: {desc}"
        return r

    def own(self, r, *expect: str, status: int | Iterable[int] = 200):
        self.checks.append(OWN)
        desc = f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:300]}"
        ok = {status} if isinstance(status, int) else set(status)
        assert r.status_code in ok, f"unexpected status: {desc}"
        leaked = self.leaked(r.text)
        assert not leaked, f"A's identifiers {leaked} in response: {desc}"
        for e in expect:
            assert e in r.text, f"own data {e!r} missing (control): {desc}"
        return r


Case = Callable[[Probe], Awaitable[None]]


@dataclass(frozen=True)
class CaseSpec:
    kind: str
    fn: Case


CASES: dict[str, CaseSpec] = {}
# Routes whose probe failed and were fixed: promoted from (c) to (a), their
# case now run by ``test_promoted_route_probe`` (cited in ``ORG_SCOPED``).
PROMOTED: dict[str, CaseSpec] = {}


def case(*routes: str, kind: str, promoted: bool = False):
    table = PROMOTED if promoted else CASES

    def deco(fn: Case) -> Case:
        for r in routes:
            assert r not in CASES and r not in PROMOTED, f"duplicate probe case for {r}"
            table[r] = CaseSpec(kind, fn)
        return fn

    return deco


def check_probe_cases(cases: dict[str, CaseSpec], probed: Iterable[str]) -> list[str]:
    """Each (c) route has exactly one case; no case for a route outside (c)."""
    probed = list(probed)
    problems = [f"{k}: (c) route has no probe case" for k in probed if k not in cases]
    problems += [
        f"{k}: probe case for a route not in (c)" for k in cases if k not in set(probed)
    ]
    problems += [
        f"{k}: unknown probe kind {s.kind!r}"
        for k, s in cases.items()
        if s.kind not in (FOREIGN, OWN)
    ]
    return problems


def check_promoted(
    promoted: Iterable[str], org_scoped: dict[str, Iterable[str]], node: str
) -> list[str]:
    """A promoted case's route sits in (a) and cites the test that runs it."""
    return [
        f"{k}: promoted probe case but (a) does not cite {node}"
        for k in promoted
        if node not in org_scoped.get(k, ())
    ]


def check_probe_run(route: str, spec: CaseSpec, hits: set[str], checks: list[str]):
    """The case hit its route template and made the check its kind requires."""
    problems = []
    if route not in hits:
        problems.append(f"{route}: probe never hit this route (hits: {sorted(hits)})")
    if spec.kind not in checks:
        problems.append(f"{route}: {spec.kind} probe made no {spec.kind} check")
    return problems


# =============================================================================
# workflow
# =============================================================================

EMPTY_DEF = {"nodes": [], "edges": []}


def wf(t) -> int:
    return t.workflow.id


@case("POST /api/v1/telephony/initiate-call", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/telephony/initiate-call"
    phone = "+886900007099"
    p.foreign(
        await p.client.post(url, json={"workflow_id": wf(p.a), "phone_number": phone})
    )
    p.foreign(
        await p.client.post(
            url,
            json={
                "workflow_id": wf(p.b),
                "phone_number": phone,
                "telephony_configuration_id": p.a.config_id,
            },
        )
    )
    p.foreign(
        await p.client.post(
            url,
            json={
                "workflow_id": wf(p.b),
                "phone_number": phone,
                "workflow_run_id": p.a.run.id,
            },
        )
    )


@case("POST /api/v1/livekit/did-mappings", kind=FOREIGN)
async def _(p: Probe):
    from api.tests.tenant_isolation.probe_world import NEW_DID

    url = f"{V1}/livekit/did-mappings"
    p.foreign(
        await p.client.post(url, json={"address": NEW_DID, "workflow_id": wf(p.a)})
    )
    p.foreign(
        await p.client.post(
            url,
            json={
                "address": p.a.number.address,
                "workflow_id": wf(p.b),
                "overwrite": True,
            },
        )
    )


@case(
    "POST /api/v1/workflow/{workflow_id}/validate",
    "POST /api/v1/workflow/{workflow_id}/publish",
    "POST /api/v1/workflow/{workflow_id}/create-draft",
    "POST /api/v1/workflow/{workflow_id}/duplicate",
    kind=FOREIGN,
)
async def _(p: Probe):
    suffix = p.extras["route"].split("{workflow_id}")[1]
    p.foreign(await p.client.post(f"{V1}/workflow/{wf(p.a)}{suffix}"))


@case(
    "GET /api/v1/workflow/fetch/{workflow_id}",
    "GET /api/v1/workflow/{workflow_id}/versions",
    "GET /api/v1/workflow/{workflow_id}/report",
    kind=FOREIGN,
)
async def _(p: Probe):
    tmpl = p.extras["route"].split(" ", 1)[1]
    p.foreign(await p.client.get(tmpl.format(workflow_id=wf(p.a))))
    p.own(await p.client.get(tmpl.format(workflow_id=wf(p.b))))


@case("POST /api/v1/workflow/create/definition", kind=OWN)
async def _(p: Probe):
    body = {"name": f"{p.b.token} new", "workflow_definition": EMPTY_DEF}
    p.own(await p.client.post(f"{V1}/workflow/create/definition", json=body), p.b.token)


@case("POST /api/v1/workflow/create/template", kind=OWN)
async def _(p: Probe):
    body = {
        "call_type": "inbound",
        "use_case": "probe",
        "activity_description": "probe",
    }
    p.own(await p.client.post(f"{V1}/workflow/create/template", json=body), p.b.token)


@case("GET /api/v1/workflow/count", kind=OWN)
async def _(p: Probe):
    r = p.own(await p.client.get(f"{V1}/workflow/count"))
    assert r.json()["total"] == 1, r.text


@case("GET /api/v1/workflow/fetch", "GET /api/v1/workflow/summary", kind=OWN)
async def _(p: Probe):
    p.own(await p.client.get(p.extras["route"].split(" ", 1)[1]), p.b.token)


@case("PUT /api/v1/workflow/{workflow_id}/status", kind=FOREIGN)
async def _(p: Probe):
    p.foreign(
        await p.client.put(
            f"{V1}/workflow/{wf(p.a)}/status", json={"status": "archived"}
        )
    )


@case("PUT /api/v1/workflow/{workflow_id}/folder", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/workflow/{{}}/folder"
    p.foreign(await p.client.put(url.format(wf(p.a)), json={"folder_id": None}))
    p.foreign(
        await p.client.put(url.format(wf(p.a)), json={"folder_id": p.b.folder.id})
    )
    p.foreign(
        await p.client.put(url.format(wf(p.b)), json={"folder_id": p.a.folder.id})
    )
    p.own(await p.client.put(url.format(wf(p.b)), json={"folder_id": p.b.folder.id}))


@case("PUT /api/v1/workflow/{workflow_id}", kind=FOREIGN)
async def _(p: Probe):
    body = {"name": "renamed by B", "workflow_definition": EMPTY_DEF}
    p.foreign(await p.client.put(f"{V1}/workflow/{wf(p.a)}", json=body))
    p.foreign(
        await p.client.put(f"{V1}/workflow/{wf(p.a)}", json={"name": "renamed by B"})
    )


@case("POST /api/v1/workflow/{workflow_id}/runs", kind=FOREIGN)
async def _(p: Probe):
    p.foreign(
        await p.client.post(
            f"{V1}/workflow/{wf(p.a)}/runs", json={"mode": "textchat", "name": "by B"}
        ),
        server_error_ok=(
            "create_workflow_run raises ValueError for another org's workflow "
            "before any insert; the route does not map it to 404"
        ),
    )


@case("GET /api/v1/workflow/{workflow_id}/runs/{run_id}", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/workflow/{{}}/runs/{{}}"
    p.foreign(await p.client.get(url.format(wf(p.a), p.a.run.id)))
    p.foreign(await p.client.get(url.format(wf(p.b), p.a.run.id)))
    p.own(await p.client.get(url.format(wf(p.b), p.b.run.id)), p.b.token)


@case("GET /api/v1/workflow/{workflow_id}/runs", kind=FOREIGN)
async def _(p: Probe):
    p.foreign(await p.client.get(f"{V1}/workflow/{wf(p.a)}/runs"))
    p.own(await p.client.get(f"{V1}/workflow/{wf(p.b)}/runs"), p.b.token)


@case("POST /api/v1/workflow/ambient-noise/upload-url", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/workflow/ambient-noise/upload-url"
    body = {"filename": "n.wav", "file_size": 10}
    p.foreign(await p.client.post(url, json={**body, "workflow_id": wf(p.a)}))
    r = p.own(await p.client.post(url, json={**body, "workflow_id": wf(p.b)}))
    assert f"ambient-noise/{p.b.org_id}/" in r.json()["storage_key"], r.text


# --- text chat ----------------------------------------------------------------


@case("POST /api/v1/workflow/{workflow_id}/text-chat/sessions", kind=FOREIGN)
async def _(p: Probe):
    p.foreign(
        await p.client.post(
            f"{V1}/workflow/{wf(p.a)}/text-chat/sessions", json={"name": "by B"}
        )
    )


def _chat(t_wf, t_run) -> str:
    return f"{V1}/workflow/{wf(t_wf)}/text-chat/sessions/{t_run.text_run.id}"


@case("GET /api/v1/workflow/{workflow_id}/text-chat/sessions/{run_id}", kind=FOREIGN)
async def _(p: Probe):
    p.foreign(await p.client.get(_chat(p.a, p.a)))
    p.foreign(await p.client.get(_chat(p.b, p.a)))
    p.own(await p.client.get(_chat(p.b, p.b)), p.b.token)


@case(
    "POST /api/v1/workflow/{workflow_id}/text-chat/sessions/{run_id}/messages",
    kind=FOREIGN,
)
async def _(p: Probe):
    for url in (_chat(p.a, p.a), _chat(p.b, p.a)):
        p.foreign(await p.client.post(f"{url}/messages", json={"text": "hello"}))


@case(
    "POST /api/v1/workflow/{workflow_id}/text-chat/sessions/{run_id}/rewind",
    kind=FOREIGN,
)
async def _(p: Probe):
    for url in (_chat(p.a, p.a), _chat(p.b, p.a)):
        p.foreign(await p.client.post(f"{url}/rewind", json={}))


# --- embed token ----------------------------------------------------------------


@case(
    "POST /api/v1/workflow/{workflow_id}/embed-token",
    "GET /api/v1/workflow/{workflow_id}/embed-token",
    "DELETE /api/v1/workflow/{workflow_id}/embed-token",
    kind=FOREIGN,
)
async def _(p: Probe):
    method = p.extras["route"].split(" ", 1)[0]
    kw = {"json": {"allowed_domains": ["b.example"]}} if method == "POST" else {}
    p.foreign(
        await p.client.request(method, f"{V1}/workflow/{wf(p.a)}/embed-token", **kw)
    )
    if method == "GET":
        p.own(
            await p.client.get(f"{V1}/workflow/{wf(p.b)}/embed-token"), p.b.embed.token
        )


# =============================================================================
# user / api keys / service keys
# =============================================================================


@case(
    "GET /api/v1/user/auth/user",
    "GET /api/v1/user/onboarding-state",
    "GET /api/v1/user/configurations/user/validate",
    "GET /api/v1/organizations/context",
    "GET /api/v1/organizations/telephony-providers/metadata",
    "GET /api/v1/organizations/telephony-config-warnings",
    "GET /api/v1/organizations/model-configurations/v2/defaults",
    "GET /api/v1/organizations/model-configurations/v2",
    "GET /api/v1/organizations/telephony-config",
    "GET /api/v1/organizations/usage/current-period",
    "GET /api/v1/organizations/usage/mps-credits",
    "GET /api/v1/organizations/billing/credits",
    "GET /api/v1/turn/credentials",
    "GET /api/v1/node-types",
    "GET /api/v1/node-types/{name}",
    "GET /api/v1/user/configurations/voices/{provider}",
    "GET /api/v1/organizations/reports/workflows",
    "GET /api/v1/organizations/campaign-defaults",
    kind=OWN,
)
async def _(p: Probe):
    path = p.extras["route"].split(" ", 1)[1]
    path = path.format(name="startCall", provider="elevenlabs")
    expect = {
        "/api/v1/organizations/reports/workflows": (p.b.token,),
        "/api/v1/user/configurations/voices/elevenlabs": (p.b.token,),
    }.get(path, ())
    p.own(await p.client.get(path), *expect)


@case("GET /api/v1/auth/me", kind=OWN)
async def _(p: Probe):
    p.own(await p.client.get(f"{V1}/auth/me"), p.b.user.provider_id)


@case("GET /api/v1/user/configurations/user", kind=OWN)
async def _(p: Probe):
    from api.tests.tenant_isolation.probe_world import TEST_PHONES

    p.own(await p.client.get(f"{V1}/user/configurations/user"), TEST_PHONES["b"])


@case("PUT /api/v1/user/configurations/user", kind=OWN)
async def _(p: Probe):
    p.own(
        await p.client.put(
            f"{V1}/user/configurations/user", json={"timezone": "Asia/Taipei"}
        ),
        "Asia/Taipei",
    )


@case("PUT /api/v1/user/onboarding-state", kind=OWN)
async def _(p: Probe):
    p.own(await p.client.put(f"{V1}/user/onboarding-state", json={"skipped": True}))


@case("GET /api/v1/user/api-keys", "POST /api/v1/user/api-keys", kind=OWN)
async def _(p: Probe):
    if p.extras["route"].startswith("POST"):
        r = await p.client.post(f"{V1}/user/api-keys", json={"name": f"{p.b.token} k2"})
    else:
        r = await p.client.get(f"{V1}/user/api-keys")
    p.own(r, p.b.token)


@case(
    "DELETE /api/v1/user/api-keys/{api_key_id}",
    "PUT /api/v1/user/api-keys/{api_key_id}/reactivate",
    kind=FOREIGN,
)
async def _(p: Probe):
    method, tmpl = p.extras["route"].split(" ", 1)
    p.foreign(await p.client.request(method, tmpl.format(api_key_id=p.a.api_key.id)))


@case("GET /api/v1/user/service-keys", "POST /api/v1/user/service-keys", kind=OWN)
async def _(p: Probe):
    if p.extras["route"].startswith("POST"):
        r = await p.client.post(
            f"{V1}/user/service-keys", json={"name": f"{p.b.token} s2"}
        )
    else:
        r = await p.client.get(f"{V1}/user/service-keys")
    p.own(r, p.b.token)


@case("DELETE /api/v1/user/service-keys/{service_key_id}", kind=FOREIGN)
async def _(p: Probe):
    p.foreign(
        await p.client.delete(f"{V1}/user/service-keys/{p.a.service_key_id}"),
        server_error_ok=(
            "MPS refuses another owner's key; the route's blanket except turns "
            "its own 404 into 500"
        ),
    )
    p.own(await p.client.delete(f"{V1}/user/service-keys/{p.b.service_key_id}"))


@case("PUT /api/v1/user/service-keys/{service_key_id}/reactivate", kind=FOREIGN)
async def _(p: Probe):
    p.foreign(
        await p.client.put(f"{V1}/user/service-keys/{p.a.service_key_id}/reactivate"),
        server_error_ok="501 Not Implemented for every key (MPS has no reactivation)",
    )


# =============================================================================
# campaign
# =============================================================================


def _campaign_body(t_wf, source_key, **extra):
    return {
        "name": "B campaign",
        "workflow_id": wf(t_wf),
        "source_type": "csv",
        "source_id": source_key,
        **extra,
    }


@case("POST /api/v1/campaign/create", kind=FOREIGN, promoted=True)
async def _(p: Probe):
    url = f"{V1}/campaign/create"
    p.foreign(await p.client.post(url, json=_campaign_body(p.a, p.b.csv_key)))
    p.foreign(
        await p.client.post(
            url,
            json=_campaign_body(
                p.b, p.b.csv_key, telephony_configuration_id=p.a.config_id
            ),
        )
    )
    p.foreign(await p.client.post(url, json=_campaign_body(p.b, p.a.csv_key)))
    p.own(await p.client.post(url, json=_campaign_body(p.b, p.b.csv_key)), p.b.csv_key)


@case("GET /api/v1/campaign/", kind=OWN)
async def _(p: Probe):
    p.own(await p.client.get(f"{V1}/campaign/"), p.b.token)


@case(
    "GET /api/v1/campaign/{campaign_id}",
    "GET /api/v1/campaign/{campaign_id}/runs",
    "GET /api/v1/campaign/{campaign_id}/source-download-url",
    "GET /api/v1/campaign/{campaign_id}/report",
    "GET /api/v1/campaign/{campaign_id}/progress",
    kind=FOREIGN,
)
async def _(p: Probe):
    tmpl = p.extras["route"].split(" ", 1)[1]
    p.foreign(await p.client.get(tmpl.format(campaign_id=p.a.campaign.id)))
    if not tmpl.endswith(("/report", "/progress")):
        own = p.b.csv_key if tmpl.endswith("-url") else p.b.token
        p.own(await p.client.get(tmpl.format(campaign_id=p.b.campaign.id)), own)


@case(
    "POST /api/v1/campaign/{campaign_id}/start",
    "POST /api/v1/campaign/{campaign_id}/pause",
    "POST /api/v1/campaign/{campaign_id}/resume",
    "POST /api/v1/campaign/{campaign_id}/redial",
    "PATCH /api/v1/campaign/{campaign_id}",
    kind=FOREIGN,
)
async def _(p: Probe):
    method, tmpl = p.extras["route"].split(" ", 1)
    kw = {}
    if method == "PATCH":
        kw = {"json": {"name": "renamed by B", "max_concurrency": 1}}
    elif tmpl.endswith("/redial"):
        kw = {"json": {"name": "redial by B"}}
    p.foreign(
        await p.client.request(method, tmpl.format(campaign_id=p.a.campaign.id), **kw)
    )


# =============================================================================
# credentials / tools
# =============================================================================


@case("GET /api/v1/credentials/", "GET /api/v1/tools/", kind=OWN)
async def _(p: Probe):
    p.own(await p.client.get(p.extras["route"].split(" ", 1)[1]), p.b.token)


@case("POST /api/v1/credentials/", kind=OWN)
async def _(p: Probe):
    body = {"name": f"{p.b.token} c2", "credential_type": "none", "credential_data": {}}
    p.own(await p.client.post(f"{V1}/credentials/", json=body), p.b.token)


@case(
    "GET /api/v1/credentials/{credential_uuid}",
    "PUT /api/v1/credentials/{credential_uuid}",
    "DELETE /api/v1/credentials/{credential_uuid}",
    kind=FOREIGN,
)
async def _(p: Probe):
    method, tmpl = p.extras["route"].split(" ", 1)
    kw = {"json": {"name": "renamed by B"}} if method == "PUT" else {}
    p.foreign(
        await p.client.request(
            method, tmpl.format(credential_uuid=p.a.credential.credential_uuid), **kw
        )
    )
    if method == "GET":
        p.own(
            await p.client.get(
                tmpl.format(credential_uuid=p.b.credential.credential_uuid)
            ),
            p.b.token,
        )


def _http_tool(cred_uuid: str) -> dict:
    return {
        "schema_version": 1,
        "type": "http_api",
        "config": {
            "method": "POST",
            "url": "https://example.com/b",
            "credential_uuid": cred_uuid,
        },
    }


@case("POST /api/v1/tools/", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/tools/"
    body = {"name": "B tool", "definition": _http_tool(p.a.credential.credential_uuid)}
    p.foreign(await p.client.post(url, json=body))
    body = {
        "name": f"{p.b.token} t2",
        "definition": _http_tool(p.b.credential.credential_uuid),
    }
    p.own(await p.client.post(url, json=body), p.b.token)


@case(
    "GET /api/v1/tools/{tool_uuid}",
    "POST /api/v1/tools/{tool_uuid}/mcp/refresh",
    "DELETE /api/v1/tools/{tool_uuid}",
    "POST /api/v1/tools/{tool_uuid}/unarchive",
    kind=FOREIGN,
)
async def _(p: Probe):
    method, tmpl = p.extras["route"].split(" ", 1)
    p.foreign(await p.client.request(method, tmpl.format(tool_uuid=p.a.tool.tool_uuid)))
    if method == "GET":
        p.own(await p.client.get(tmpl.format(tool_uuid=p.b.tool.tool_uuid)), p.b.token)


@case("PUT /api/v1/tools/{tool_uuid}", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/tools/{{}}"
    p.foreign(await p.client.put(url.format(p.a.tool.tool_uuid), json={"name": "by B"}))
    p.foreign(
        await p.client.put(
            url.format(p.a.tool.tool_uuid),
            json={"definition": _http_tool(p.b.credential.credential_uuid)},
        )
    )
    p.foreign(
        await p.client.put(
            url.format(p.b.tool.tool_uuid),
            json={"definition": _http_tool(p.a.credential.credential_uuid)},
        )
    )


# =============================================================================
# organization: model configuration / preferences / langfuse
# =============================================================================


@case("PUT /api/v1/organizations/model-configurations/v2", kind=OWN)
async def _(p: Probe):
    body = {"mode": "dograh", "dograh": {"api_key": f"key-{p.b.token}-0123456789"}}
    p.own(await p.client.put(f"{V1}/organizations/model-configurations/v2", json=body))


@case(
    "GET /api/v1/organizations/model-configurations/v2/migration-preview",
    "POST /api/v1/organizations/model-configurations/v2/migrate",
    kind=OWN,
)
async def _(p: Probe):
    method, path = p.extras["route"].split(" ", 1)
    p.own(await p.client.request(method, path))


@case(
    "GET /api/v1/organizations/preferences",
    "GET /api/v1/organizations/model-configurations/preferences",
    kind=OWN,
)
async def _(p: Probe):
    from api.tests.tenant_isolation.probe_world import TEST_PHONES

    p.own(await p.client.get(p.extras["route"].split(" ", 1)[1]), TEST_PHONES["b"])


@case(
    "PUT /api/v1/organizations/preferences",
    "PUT /api/v1/organizations/model-configurations/preferences",
    kind=OWN,
)
async def _(p: Probe):
    body = {"test_phone_number": "+886900007003", "timezone": "Asia/Taipei"}
    p.own(
        await p.client.put(p.extras["route"].split(" ", 1)[1], json=body),
        "+886900007003",
    )


@case(
    "GET /api/v1/organizations/langfuse-credentials",
    "POST /api/v1/organizations/langfuse-credentials",
    "DELETE /api/v1/organizations/langfuse-credentials",
    kind=OWN,
)
async def _(p: Probe):
    method, path = p.extras["route"].split(" ", 1)
    if method == "POST":
        body = {
            "host": "https://b2.example",
            "public_key": "pk-b2",
            "secret_key": "sk-b2",
        }
        p.own(await p.client.post(path, json=body))
        p.own(await p.client.get(path), "https://b2.example")
    elif method == "GET":
        p.own(await p.client.get(path), p.b.token)
    else:
        p.own(await p.client.delete(path))


# =============================================================================
# organization: telephony configurations and phone numbers
# =============================================================================

TWILIO = {"provider": "twilio", "account_sid": "AC-probe-b", "auth_token": "tok-b"}


@case("GET /api/v1/organizations/telephony-configs", kind=OWN)
async def _(p: Probe):
    p.own(await p.client.get(f"{V1}/organizations/telephony-configs"), p.b.token)


@case("POST /api/v1/organizations/telephony-configs", kind=OWN)
async def _(p: Probe):
    body = {"name": f"{p.b.token} tw", "config": TWILIO}
    p.own(
        await p.client.post(f"{V1}/organizations/telephony-configs", json=body),
        p.b.token,
    )


@case(
    "GET /api/v1/organizations/telephony-configs/{config_id}",
    "PUT /api/v1/organizations/telephony-configs/{config_id}",
    "POST /api/v1/organizations/telephony-configs/{config_id}/set-default-outbound",
    "DELETE /api/v1/organizations/telephony-configs/{config_id}",
    "GET /api/v1/organizations/telephony-configs/{config_id}/phone-numbers",
    kind=FOREIGN,
)
async def _(p: Probe):
    method, tmpl = p.extras["route"].split(" ", 1)
    kw = {"json": {"name": "renamed by B"}} if method == "PUT" else {}
    p.foreign(
        await p.client.request(method, tmpl.format(config_id=p.a.config_id), **kw)
    )
    if method == "GET":
        own = p.b.number.address if tmpl.endswith("phone-numbers") else p.b.token
        p.own(await p.client.get(tmpl.format(config_id=p.b.config_id)), own)


@case(
    "POST /api/v1/organizations/telephony-configs/{config_id}/phone-numbers",
    kind=FOREIGN,
)
async def _(p: Probe):
    from api.tests.tenant_isolation.probe_world import NEW_DID

    url = f"{V1}/organizations/telephony-configs/{{}}/phone-numbers"
    p.foreign(await p.client.post(url.format(p.a.config_id), json={"address": NEW_DID}))
    p.foreign(
        await p.client.post(
            url.format(p.b.config_id),
            json={"address": NEW_DID, "inbound_workflow_id": wf(p.a)},
        )
    )
    p.foreign(
        await p.client.post(
            url.format(p.b.config_id), json={"address": p.a.number.address}
        )
    )


def _num_urls(p: Probe, tmpl: str) -> list[str]:
    return [
        tmpl.format(config_id=p.a.config_id, phone_number_id=p.a.number.id),
        tmpl.format(config_id=p.b.config_id, phone_number_id=p.a.number.id),
    ]


@case(
    "GET /api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}",
    "POST /api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}/set-default-caller",
    "DELETE /api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}",
    kind=FOREIGN,
)
async def _(p: Probe):
    method, tmpl = p.extras["route"].split(" ", 1)
    for url in _num_urls(p, tmpl):
        p.foreign(await p.client.request(method, url))
    if method == "GET":
        own = tmpl.format(config_id=p.b.config_id, phone_number_id=p.b.number.id)
        p.own(await p.client.get(own), p.b.number.address)


@case(
    "PUT /api/v1/organizations/telephony-configs/{config_id}/phone-numbers/{phone_number_id}",
    kind=FOREIGN,
)
async def _(p: Probe):
    tmpl = p.extras["route"].split(" ", 1)[1]
    for url in _num_urls(p, tmpl):
        p.foreign(await p.client.put(url, json={"label": "by B"}))
    own = tmpl.format(config_id=p.b.config_id, phone_number_id=p.b.number.id)
    p.foreign(await p.client.put(own, json={"inbound_workflow_id": wf(p.a)}))


@case("POST /api/v1/organizations/telephony-config", kind=FOREIGN)
async def _(p: Probe):
    body = {**TWILIO, "from_numbers": [p.a.number.address, "+886221117777"]}
    p.foreign(await p.client.post(f"{V1}/organizations/telephony-config", json=body))


# =============================================================================
# storage (s3) / knowledge base / workflow recordings
# =============================================================================


@case("GET /api/v1/s3/signed-url", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/s3/signed-url"
    for key in (p.a.csv_key, p.a.doc_key, f"recordings/{p.a.run.id}.wav"):
        p.foreign(await p.client.get(url, params={"key": key}))
    p.own(await p.client.get(url, params={"key": p.b.csv_key}), p.b.csv_key)


@case("POST /api/v1/s3/presigned-upload-url", kind=OWN)
async def _(p: Probe):
    body = {"file_name": "list.csv", "file_size": 10}
    r = p.own(await p.client.post(f"{V1}/s3/presigned-upload-url", json=body))
    assert r.json()["file_key"].startswith(f"campaigns/{p.b.org_id}/"), r.text


@case("POST /api/v1/knowledge-base/upload-url", kind=OWN)
async def _(p: Probe):
    body = {"filename": "doc.pdf", "mime_type": "application/pdf"}
    r = p.own(await p.client.post(f"{V1}/knowledge-base/upload-url", json=body))
    assert r.json()["s3_key"].startswith(f"knowledge_base/{p.b.org_id}/"), r.text


@case("POST /api/v1/knowledge-base/process-document", kind=FOREIGN, promoted=True)
async def _(p: Probe):
    import uuid

    url = f"{V1}/knowledge-base/process-document"
    p.foreign(
        await p.client.post(
            url, json={"document_uuid": str(uuid.uuid4()), "s3_key": p.a.doc_key}
        )
    )
    new = str(uuid.uuid4())
    own_key = f"knowledge_base/{p.b.org_id}/{new}/{p.b.token}-2.pdf"
    p.own(
        await p.client.post(url, json={"document_uuid": new, "s3_key": own_key}),
        p.b.token,
    )


@case("GET /api/v1/knowledge-base/documents", kind=OWN)
async def _(p: Probe):
    p.own(await p.client.get(f"{V1}/knowledge-base/documents"), p.b.token)


@case(
    "GET /api/v1/knowledge-base/documents/{document_uuid}",
    "DELETE /api/v1/knowledge-base/documents/{document_uuid}",
    kind=FOREIGN,
)
async def _(p: Probe):
    method, tmpl = p.extras["route"].split(" ", 1)
    p.foreign(
        await p.client.request(
            method, tmpl.format(document_uuid=p.a.document.document_uuid)
        )
    )
    if method == "GET":
        p.own(
            await p.client.get(tmpl.format(document_uuid=p.b.document.document_uuid)),
            p.b.token,
        )


@case("POST /api/v1/knowledge-base/search", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/knowledge-base/search"
    p.foreign(
        await p.client.post(
            url, json={"query": "q", "document_uuids": [p.a.document.document_uuid]}
        )
    )
    p.own(await p.client.post(url, json={"query": "q"}), p.b.token)


@case("POST /api/v1/workflow-recordings/upload-url", kind=OWN)
async def _(p: Probe):
    body = {"files": [{"filename": "r.wav", "file_size": 10}]}
    r = p.own(await p.client.post(f"{V1}/workflow-recordings/upload-url", json=body))
    assert f"recordings/{p.b.org_id}/" in r.json()["items"][0]["storage_key"], r.text


@case("POST /api/v1/workflow-recordings/", kind=FOREIGN, promoted=True)
async def _(p: Probe):
    url = f"{V1}/workflow-recordings/"

    def body(key):
        return {
            "recordings": [
                {"recording_id": "brec1", "transcript": "t", "storage_key": key}
            ]
        }

    p.foreign(await p.client.post(url, json=body(p.a.recording.storage_key)))
    own_key = f"recordings/{p.b.org_id}/brec1/{p.b.token}-2.wav"
    p.own(await p.client.post(url, json=body(own_key)), p.b.token)


@case("GET /api/v1/workflow-recordings/", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/workflow-recordings/"
    p.foreign(await p.client.get(url, params={"workflow_id": wf(p.a)}))
    p.own(await p.client.get(url), p.b.token)


@case("DELETE /api/v1/workflow-recordings/{recording_id}", kind=FOREIGN)
async def _(p: Probe):
    p.foreign(
        await p.client.delete(f"{V1}/workflow-recordings/{p.a.recording.recording_id}")
    )


@case("PATCH /api/v1/workflow-recordings/{id}", kind=FOREIGN)
async def _(p: Probe):
    p.foreign(
        await p.client.patch(
            f"{V1}/workflow-recordings/{p.a.recording.id}", json={"recording_id": "byB"}
        )
    )


@case("POST /api/v1/workflow-recordings/transcribe", kind=OWN)
async def _(p: Probe):
    r = await p.client.post(
        f"{V1}/workflow-recordings/transcribe",
        files={"file": ("a.wav", b"RIFF0000", "audio/wav")},
        data={"language": "en"},
    )
    p.own(r, p.b.token)


# =============================================================================
# folders
# =============================================================================


@case("GET /api/v1/folder/", kind=OWN)
async def _(p: Probe):
    p.own(await p.client.get(f"{V1}/folder/"), p.b.token)


@case("POST /api/v1/folder/", kind=OWN)
async def _(p: Probe):
    p.own(
        await p.client.post(f"{V1}/folder/", json={"name": f"{p.b.token} f2"}),
        p.b.token,
    )


@case(
    "PUT /api/v1/folder/{folder_id}", "DELETE /api/v1/folder/{folder_id}", kind=FOREIGN
)
async def _(p: Probe):
    method, tmpl = p.extras["route"].split(" ", 1)
    kw = {"json": {"name": "renamed by B"}} if method == "PUT" else {}
    p.foreign(
        await p.client.request(method, tmpl.format(folder_id=p.a.folder.id), **kw)
    )


# =============================================================================
# usage / reports
# =============================================================================


def _filters(attr: str, value: int) -> str:
    import json

    return json.dumps(
        [{"attribute": attr, "type": "number", "value": {"value": value}}]
    )


RANGE = {
    "start_date": "2021-03-01T00:00:00+00:00",
    "end_date": "2021-03-31T00:00:00+00:00",
}


@case(
    "GET /api/v1/organizations/usage/runs",
    "GET /api/v1/organizations/usage/runs/report",
    kind=FOREIGN,
)
async def _(p: Probe):
    path = p.extras["route"].split(" ", 1)[1]
    for attr, value in (
        ("runId", p.a.run.id),
        ("workflowId", wf(p.a)),
        ("campaignId", p.a.campaign.id),
    ):
        p.foreign(
            await p.client.get(path, params={**RANGE, "filters": _filters(attr, value)})
        )
    p.own(
        await p.client.get(path, params=RANGE),
        p.b.token if path.endswith("runs") else "",
    )


@case("GET /api/v1/organizations/usage/daily-breakdown", kind=OWN)
async def _(p: Probe):
    p.own(await p.client.get(f"{V1}/organizations/usage/daily-breakdown"))


@case("POST /api/v1/organizations/usage/mps-credits/purchase-url", kind=OWN)
async def _(p: Probe):
    # OSS deployment: purchases are off for every organization (404, no data).
    p.own(
        await p.client.post(f"{V1}/organizations/usage/mps-credits/purchase-url"),
        status=404,
    )


DAY = {"date": "2021-03-10", "timezone": "UTC"}


@case("GET /api/v1/organizations/reports/daily", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/organizations/reports/daily"
    r = p.foreign(await p.client.get(url, params={**DAY, "workflow_id": wf(p.a)}))
    own = p.own(await p.client.get(url, params={**DAY, "workflow_id": wf(p.b)}))
    # Reports carry counts, not names: A's workflow filter SHALL count nothing
    # while the same filter on B's own workflow counts B's run (control).
    if r.status_code == 200:
        assert sum(r.json()["metrics"].values()) == 0, r.text
    assert sum(own.json()["metrics"].values()) > 0, own.text


@case("GET /api/v1/organizations/reports/daily/runs", kind=FOREIGN)
async def _(p: Probe):
    url = f"{V1}/organizations/reports/daily/runs"
    p.foreign(await p.client.get(url, params={**DAY, "workflow_id": wf(p.a)}))
    p.own(await p.client.get(url, params={**DAY, "workflow_id": wf(p.b)}), p.b.token)


# =============================================================================
# WebRTC signaling websocket
# =============================================================================


@case(
    "WS /api/v1/ws/signaling/{workflow_id}/{workflow_run_id}",
    kind=FOREIGN,
    promoted=True,
)
async def _(p: Probe):
    calls = p.extras["signaling"].calls
    url = f"{V1}/ws/signaling/{{}}/{{}}"
    for wf_t, run in ((p.a, p.a.run), (p.b, p.a.run), (p.a, p.b.run)):
        res = await p.ws(url.format(wf(wf_t), run.id))
        p.checks.append(FOREIGN)
        assert not res["accepted"], f"{wf_t.tag} workflow + run {run.id}: {res} {calls}"
        leaked = p.leaked(repr(res["messages"]))
        assert not leaked, res
    assert calls == [], f"signaling started for another org's workflow/run: {calls}"
    res = await p.ws(url.format(wf(p.b), p.b.run.id))
    assert res["accepted"], f"own workflow/run refused (control): {res}"
    assert calls == [(wf(p.b), p.b.run.id, p.b.user.id)], calls
