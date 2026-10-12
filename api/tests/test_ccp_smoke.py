"""Smoke-call recognition (local-deploy-sip-smoke tasks 2.1–2.3, design D2 r2).

Only a room named by the smoke dispatch rule (``cs-zzsmoke-…``) is a smoke
call: the run is tagged (and excluded by call_scope) and its alerts stay
log-only. Anything else -- a ``ccp.smoke`` attribute, caller-controlled fields
that merely look like a smoke call, a production ``cs-_…`` room -- is a
production call.
"""

import asyncio
import types

import pytest

from api.services.ccp import call_scope, smoke
from api.services.observability import alerts

SMOKE_ROOM = "cs-zzsmoke-_+886299990001_abc"
PROD_ROOM = "cs-_+886212345678_abc"


class _FakeRedis:
    def __init__(self):
        self.counts: dict[str, int] = {}

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, seconds):
        pass

    async def delete(self, key):
        self.counts.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(alerts, "_redis", r)
    return r


@pytest.fixture
def sent(monkeypatch):
    captured = []

    async def fake_send(url, text):
        captured.append(text)

    monkeypatch.setattr(alerts, "_send", fake_send)
    monkeypatch.setenv("OBS_ALERT_WEBHOOK_URL", "https://hooks.example/x")
    monkeypatch.setenv("OBS_ERROR_THRESHOLD", "1")
    return captured


async def _drain():
    for _ in range(10):
        await asyncio.sleep(0)


# --- recognition ---


@pytest.mark.parametrize(
    "room, expected",
    [
        (SMOKE_ROOM, True),
        ("cs-zzsmoke-", True),
        (PROD_ROOM, False),
        ("cs-+886212345678", False),
        # the called number follows the rule's prefix; it can never spell the smoke prefix
        ("cs-_zzsmoke-x", False),
        ("queue-zzsmoke-_+886299990003_x", False),
        ("", False),
        (None, False),
    ],
)
def test_is_smoke_room_only_on_smoke_rule_prefix(room, expected):
    assert smoke.is_smoke_room(room) is expected


# --- 2.1 dispatch tags the run ---


def _wire_dispatch(monkeypatch, created):
    from api.db import db_client
    from api.services.pipecat import livekit_dispatcher, run_pipeline
    from api.tests.support.answer_fakes import install

    install(monkeypatch, [])

    async def fake_create(**kw):
        created.append(kw)
        return types.SimpleNamespace(id=77)

    async def fake_pipeline(**kw):
        pass

    monkeypatch.delenv("LIVEKIT_MAX_CONCURRENT_CALLS", raising=False)
    monkeypatch.setattr(db_client, "create_workflow_run", fake_create)
    monkeypatch.setattr(run_pipeline, "run_pipeline_livekit", fake_pipeline)
    monkeypatch.setattr(livekit_dispatcher, "_sign_agent_token", lambda r, i: "jwt")
    monkeypatch.setenv("LIVEKIT_URL", "ws://test")
    return livekit_dispatcher


async def _resolver(did):
    return (1, 2)


async def _fallback_fails(room, reason, workflow_run_id=None):
    raise AssertionError(f"fallback must not fire: {reason}")


@pytest.mark.asyncio
async def test_smoke_room_dispatch_tags_run_without_attribute(monkeypatch):
    # task 0.3: the webhook may lack the rule's attributes -- the room name decides
    created = []
    d = _wire_dispatch(monkeypatch, created)
    await d.dispatch_livekit_call(
        SMOKE_ROOM,
        {"sip.trunkPhoneNumber": "+886299990001"},
        _resolver,
        _fallback_fails,
    )
    await _drain()
    assert created[0]["initial_context"]["ccp_smoke"] is True


@pytest.mark.asyncio
async def test_production_room_untagged_even_with_smoke_attribute(monkeypatch):
    created = []
    d = _wire_dispatch(monkeypatch, created)
    await d.dispatch_livekit_call(
        PROD_ROOM,
        {
            "sip.trunkPhoneNumber": "+886212345678",
            "ccp.smoke": "1",
            "sip.phoneNumber": "+886900000101",
        },
        _resolver,
        _fallback_fails,
    )
    await _drain()
    assert created[0]["initial_context"]["ccp_smoke"] is False


# --- 2.2 call_scope excludes smoke runs ---


def test_scope_clause_excludes_smoke_runs():
    clause = call_scope.scope_clause("wr.")
    assert "COALESCE(wr.initial_context->>'ccp_smoke', '') <> 'true'" in clause


# --- 2.3 alerts stay log-only for smoke rooms ---


@pytest.mark.asyncio
async def test_immediate_alert_suppressed_for_smoke_room(fake_redis, sent):
    alerts.notify(
        "safetynet.triggered", {"room_name": SMOKE_ROOM, "reason": "unmapped_did"}
    )
    await _drain()
    assert sent == []


@pytest.mark.asyncio
async def test_immediate_alert_still_sent_for_production_room(fake_redis, sent):
    alerts.notify(
        "safetynet.triggered", {"room_name": PROD_ROOM, "reason": "unmapped_did"}
    )
    await _drain()
    assert len(sent) == 1 and PROD_ROOM in sent[0]


@pytest.mark.asyncio
async def test_windowed_alert_not_counted_for_smoke_room(fake_redis, sent):
    alerts.notify("capacity.rejected", {"room_name": SMOKE_ROOM, "reason": "full"})
    await _drain()
    assert sent == [] and fake_redis.counts == {}


# --- security M1: only the dispatcher writes the run tag ---


def test_without_reserved_strips_the_tag_only():
    assert smoke.without_reserved({"ccp_smoke": True, "name": "x"}) == {"name": "x"}
    assert smoke.without_reserved(None) == {}


@pytest.mark.asyncio
async def test_template_variable_cannot_tag_a_run(db_session, async_session):
    from api.db.models import OrganizationModel, UserModel

    org = OrganizationModel(provider_id="smoke-m1-org")
    async_session.add(org)
    await async_session.flush()
    user = UserModel(provider_id="smoke-m1-user", selected_organization_id=org.id)
    async_session.add(user)
    await async_session.flush()
    wf = await db_session.create_workflow(
        name="m1",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org.id,
    )
    wf.template_context_variables = {"ccp_smoke": True, "greeting": "hi"}
    await async_session.flush()
    run = await db_session.create_workflow_run(
        name="m1-run",
        workflow_id=wf.id,
        mode="livekit",
        user_id=user.id,
        initial_context={"did": "+886212345678"},
    )
    assert "ccp_smoke" not in run.initial_context
    assert run.initial_context["greeting"] == "hi"
