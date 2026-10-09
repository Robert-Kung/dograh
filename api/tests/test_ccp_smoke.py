"""Smoke-call recognition (local-deploy-sip-smoke tasks 2.1–2.3).

Only the SIP participant's server-set ``ccp.smoke=1`` marks a smoke call: the
run is tagged (and excluded by call_scope), the room is registered so its
alerts stay log-only. Anything else -- missing attribute, caller-controlled
fields that merely look like a smoke call -- is a production call.
"""

import asyncio
import types

import pytest

from api.services.ccp import call_scope, smoke
from api.services.observability import alerts


class _FakeRedis:
    def __init__(self):
        self.keys: dict[str, tuple[str, int | None]] = {}
        self.counts: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.keys[key] = (value, ex)

    async def exists(self, key):
        return int(key in self.keys)

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, seconds):
        pass

    async def delete(self, key):
        self.counts.pop(key, None)


class _BrokenRedis:
    async def set(self, *a, **k):
        raise ConnectionError("down")

    async def exists(self, *a, **k):
        raise ConnectionError("down")


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(smoke, "_redis", r)
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
    "attrs, expected",
    [
        ({"ccp.smoke": "1"}, True),
        ({"ccp.smoke": "true"}, False),
        ({"ccp.smoke": "0"}, False),
        ({}, False),
        (None, False),
        # a caller-controlled header lands under sip.h.*, never ccp.*
        ({"sip.h.ccp.smoke": "1", "sip.h.x-ccp-smoke": "1"}, False),
        ({"sip.phoneNumber": "+886911000001"}, False),
    ],
)
def test_is_smoke_only_on_exact_server_attribute(attrs, expected):
    assert smoke.is_smoke(attrs) is expected


@pytest.mark.asyncio
async def test_mark_and_lookup_room(fake_redis):
    await smoke.mark_room("cs-x")
    assert await smoke.is_smoke_room("cs-x")
    assert not await smoke.is_smoke_room("cs-y")
    assert not await smoke.is_smoke_room(None)
    assert fake_redis.keys["ccp:smoke_room:cs-x"][1] == smoke.ROOM_TTL_SECONDS


@pytest.mark.asyncio
async def test_redis_failure_reads_as_production(monkeypatch):
    monkeypatch.setattr(smoke, "_redis", _BrokenRedis())
    await smoke.mark_room("cs-x")  # must not raise
    assert await smoke.is_smoke_room("cs-x") is False


# --- 2.1 dispatch tags the run and registers the room ---


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
async def test_smoke_dispatch_tags_run_and_marks_room(monkeypatch, fake_redis):
    created = []
    d = _wire_dispatch(monkeypatch, created)
    await d.dispatch_livekit_call(
        "cs-+886212345000",
        {"sip.trunkPhoneNumber": "+886212345000", "ccp.smoke": "1"},
        _resolver,
        _fallback_fails,
    )
    await _drain()
    assert created[0]["initial_context"]["smoke"] is True
    assert await smoke.is_smoke_room("cs-+886212345000")


@pytest.mark.asyncio
async def test_production_dispatch_untagged_even_with_smoke_caller_number(
    monkeypatch, fake_redis
):
    created = []
    d = _wire_dispatch(monkeypatch, created)
    await d.dispatch_livekit_call(
        "cs-+886212345678",
        {"sip.trunkPhoneNumber": "+886212345678", "sip.phoneNumber": "+886911000001"},
        _resolver,
        _fallback_fails,
    )
    await _drain()
    assert "smoke" not in created[0]["initial_context"]
    assert not await smoke.is_smoke_room("cs-+886212345678")


@pytest.mark.asyncio
async def test_unmapped_smoke_did_marks_room_before_fallback(fake_redis):
    from api.services.pipecat.livekit_dispatcher import dispatch_livekit_call

    seen = {}

    async def resolver(did):
        return None

    async def fallback(room, reason, workflow_run_id=None):
        seen["smoke_at_fallback"] = await smoke.is_smoke_room(room)

    await dispatch_livekit_call(
        "cs-+886200000000",
        {"sip.trunkPhoneNumber": "+886200000000", "ccp.smoke": "1"},
        resolver,
        fallback,
    )
    assert seen == {"smoke_at_fallback": True}


# --- 2.2 call_scope excludes smoke runs ---


def test_scope_clause_excludes_smoke_runs():
    clause = call_scope.scope_clause("wr.")
    assert "COALESCE(wr.initial_context->>'smoke', '') <> 'true'" in clause


# --- 2.3 alerts stay log-only for smoke rooms ---


@pytest.mark.asyncio
async def test_immediate_alert_suppressed_for_smoke_room(fake_redis, sent):
    await smoke.mark_room("cs-smoke")
    alerts.notify("safetynet.triggered", {"room_name": "cs-smoke", "reason": "unmapped_did"})
    await _drain()
    assert sent == []


@pytest.mark.asyncio
async def test_immediate_alert_still_sent_for_production_room(fake_redis, sent):
    alerts.notify("safetynet.triggered", {"room_name": "cs-prod", "reason": "unmapped_did"})
    await _drain()
    assert len(sent) == 1 and "cs-prod" in sent[0]


@pytest.mark.asyncio
async def test_windowed_alert_not_counted_for_smoke_room(fake_redis, sent):
    await smoke.mark_room("cs-smoke")
    alerts.notify("capacity.rejected", {"room_name": "cs-smoke", "reason": "full"})
    await _drain()
    assert sent == [] and fake_redis.counts == {}


@pytest.mark.asyncio
async def test_alert_sent_when_smoke_lookup_fails(monkeypatch, sent):
    monkeypatch.setattr(smoke, "_redis", _BrokenRedis())
    alerts.notify("safetynet.triggered", {"room_name": "cs-smoke", "reason": "x"})
    await _drain()
    assert len(sent) == 1


# --- reconciler path ---


@pytest.mark.asyncio
async def test_reconciler_marks_smoke_room(monkeypatch, fake_redis):
    from api.services.pipecat import livekit_dispatcher, livekit_safetynet
    from api.services.pipecat.livekit_cold_transfer import SIP_KIND

    caller = types.SimpleNamespace(
        identity="sip_1", kind=SIP_KIND, joined_at=0,
        attributes={"ccp.smoke": "1", "sip.callID": "c-1"}, sid="PA_1",
    )
    room = types.SimpleNamespace(name="cs-+886212345000")

    class _Room:
        async def list_rooms(self, req):
            return types.SimpleNamespace(rooms=[room])

        async def list_participants(self, req):
            return types.SimpleNamespace(participants=[caller])

    handed = []

    async def fake_safetynet(room_name, reason, run_id, lk=None):
        handed.append((room_name, reason, await smoke.is_smoke_room(room_name)))

    monkeypatch.setattr(livekit_safetynet, "server_side_safetynet", fake_safetynet)
    client = types.SimpleNamespace(room=_Room())
    n = await livekit_safetynet.reconcile_undispatched_rooms(
        client, now=10**10, dedup=livekit_dispatcher.DispatchDedup()
    )
    assert n == 1 and handed == [("cs-+886212345000", "undispatched", True)]
