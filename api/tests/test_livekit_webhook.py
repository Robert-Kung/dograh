"""LiveKit inbound webhook (livekit-event-wiring §2.1–2.3).

Trigger is the SIP caller's ``participant_joined`` in a ``cs-`` room; the
route carries a dograh-only path secret; one call dispatches once (two-phase
dedup keyed by ``sip.callID``); every failure is handed to the fallback.
"""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from livekit.protocol.models import ParticipantInfo, Room
from livekit.protocol.webhook import WebhookEvent

from api.routes import livekit as livekit_route
from api.services.pipecat import livekit_dispatcher
from api.services.pipecat.livekit_dispatcher import DispatchDedup
from api.utils import background

SECRET = "s3cr3t-path-value-0123456789abcdef0123456789"
ROOM = "cs-_+886212345678_gUGzsyAS2QVG"
SIP_ATTRS = {
    "sip.callID": "SCL_2FZTC6aLeMs4",
    "sip.phoneNumber": "+886911000001",
    "sip.trunkPhoneNumber": "+886212345678",
    "sip.trunkID": "ST_ai",
    "sip.ruleID": "SDR_ai",
}


def _event(
    kind="participant_joined",
    room=ROOM,
    pkind=ParticipantInfo.Kind.SIP,
    attrs=None,
    sid="PA_sip",
):
    ev = WebhookEvent(event=kind, room=Room(name=room))
    if kind.startswith("participant"):
        ev.participant.CopyFrom(
            ParticipantInfo(
                sid=sid,
                identity="sip_+886911000001",
                kind=pkind,
                attributes=SIP_ATTRS if attrs is None else attrs,
            )
        )
    return ev


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv(livekit_route.WEBHOOK_PATH_SECRET_ENV, SECRET)
    monkeypatch.setattr(livekit_dispatcher, "dispatch_dedup", DispatchDedup())
    monkeypatch.setattr(livekit_dispatcher, "RESOLVER_RETRY_DELAYS_SECONDS", (0, 0))
    monkeypatch.setattr(
        livekit_dispatcher, "_rejected", {"last_emit": None, "suppressed": 0}
    )
    a = FastAPI()
    a.include_router(livekit_route.router, prefix="/api/v1")
    return a


@pytest.fixture
def calls(monkeypatch):
    """Record dispatch outcomes without DB / pipeline / LiveKit."""
    rec = {"resolver": 0, "launched": [], "fallback": [], "resolver_errors": 0}

    async def resolver(did):
        rec["resolver"] += 1
        if rec["resolver_errors"] > 0:
            rec["resolver_errors"] -= 1
            raise ConnectionError("db blip")
        rec["did"] = did
        return (11, 22)

    async def fallback(room, reason, workflow_run_id=None):
        rec["fallback"].append(reason)

    async def launch(room, attrs, resolver_, fallback_, livekit_url):
        did = livekit_dispatcher.did_from_sip_attributes(attrs)
        if not did:
            await fallback_(room, "no_did", None)
            return True
        resolved = await livekit_dispatcher._resolve_with_retry(resolver_, did)
        if not resolved:
            await fallback_(room, "unmapped_did", None)
            return True
        rec["launched"].append(room)
        return True

    monkeypatch.setattr(livekit_dispatcher, "_dispatch", launch)
    monkeypatch.setattr(livekit_route, "_did_resolver", resolver)
    monkeypatch.setattr(livekit_route, "_fallback", fallback)
    return rec


async def _post(
    app,
    path=f"/api/v1/livekit/inbound/{SECRET}",
    event=None,
    verify=None,
    monkeypatch=None,
):
    if monkeypatch is not None:
        monkeypatch.setattr(
            livekit_route,
            "_verify",
            verify or (lambda body, auth: event),
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dograh-api:8000"
    ) as client:
        resp = await client.post(path, content=b"{}", headers={"Authorization": "x"})
    await _drain()
    return resp


async def _drain():
    for _ in range(10):
        pending = [t for t in background._background_tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


# --- path secret ------------------------------------------------------------


async def test_wrong_path_secret_is_404(app, calls, monkeypatch):
    r = await _post(
        app, "/api/v1/livekit/inbound/wrong", event=_event(), monkeypatch=monkeypatch
    )
    assert r.status_code == 404
    assert calls["launched"] == []


async def test_old_secretless_path_is_gone(app, calls, monkeypatch):
    r = await _post(
        app, "/api/v1/livekit/inbound", event=_event(), monkeypatch=monkeypatch
    )
    assert r.status_code == 404
    assert calls["launched"] == []


async def test_unset_secret_is_404_even_for_empty_path(app, calls, monkeypatch):
    monkeypatch.delenv(livekit_route.WEBHOOK_PATH_SECRET_ENV)
    r = await _post(
        app,
        f"/api/v1/livekit/inbound/{SECRET}",
        event=_event(),
        monkeypatch=monkeypatch,
    )
    assert r.status_code == 404


def test_path_secret_comparison_is_constant_time(monkeypatch):
    seen = []
    monkeypatch.setenv(livekit_route.WEBHOOK_PATH_SECRET_ENV, SECRET)
    monkeypatch.setattr(
        livekit_route.secrets,
        "compare_digest",
        lambda a, b: seen.append((a, b)) or a == b,
    )
    assert livekit_route._path_secret_matches(SECRET)
    assert seen == [(SECRET.encode(), SECRET.encode())]
    # Non-ASCII input must not raise (str compare_digest would).
    assert not livekit_route._path_secret_matches("秘密")


# --- signature --------------------------------------------------------------


async def test_bad_signature_is_401_with_rate_limited_event(app, calls, monkeypatch):
    emitted = []
    monkeypatch.setattr(
        "api.services.observability.call_events.emit",
        lambda event, **kw: emitted.append((event, kw)),
    )

    def bad(body, auth):
        raise ValueError("signature mismatch")

    r1 = await _post(app, verify=bad, monkeypatch=monkeypatch)
    r2 = await _post(app, verify=bad, monkeypatch=monkeypatch)
    assert r1.status_code == r2.status_code == 401
    assert [e for e, _ in emitted] == [livekit_dispatcher.WEBHOOK_REJECTED_EVENT]
    assert livekit_dispatcher._rejected["suppressed"] == 1
    assert calls["launched"] == []


def test_rejected_event_after_window_reports_folded_count(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        "api.services.observability.call_events.emit",
        lambda event, **kw: emitted.append(kw),
    )
    monkeypatch.setattr(
        livekit_dispatcher, "_rejected", {"last_emit": None, "suppressed": 0}
    )
    t = [0.0]
    for _ in range(4):
        livekit_dispatcher.record_webhook_rejected(ValueError(), clock=lambda: t[0])
    t[0] = livekit_dispatcher.WEBHOOK_REJECTED_WINDOW_SECONDS + 1
    livekit_dispatcher.record_webhook_rejected(ValueError(), clock=lambda: t[0])
    assert [e["suppressed"] for e in emitted] == [0, 3]


# --- event routing ----------------------------------------------------------


async def test_sip_participant_joined_dispatches(app, calls, monkeypatch):
    r = await _post(app, event=_event(), monkeypatch=monkeypatch)
    assert r.status_code == 200
    assert calls["launched"] == [ROOM]
    assert calls["did"] == "+886212345678"


async def test_national_format_dialed_number_resolves_e164(app, calls, monkeypatch):
    attrs = {**SIP_ATTRS, "sip.trunkPhoneNumber": "0212345678"}
    await _post(
        app, event=_event(room="cs-_0212345678_x", attrs=attrs), monkeypatch=monkeypatch
    )
    assert calls["did"] == "+886212345678"


async def test_room_started_does_not_dispatch(app, calls, monkeypatch):
    r = await _post(app, event=_event(kind="room_started"), monkeypatch=monkeypatch)
    assert r.status_code == 200
    assert calls["launched"] == [] and calls["resolver"] == 0


async def test_agent_join_does_not_dispatch(app, calls, monkeypatch):
    ev = _event(pkind=ParticipantInfo.Kind.STANDARD, attrs={})
    r = await _post(app, event=ev, monkeypatch=monkeypatch)
    assert r.status_code == 200
    assert calls["launched"] == []


def test_non_cs_room_attributes_are_never_read():
    """Data minimization (security L5): the queue's rooms' caller attributes
    must not even be touched here."""

    class Untouchable:
        kind = ParticipantInfo.Kind.SIP

        @property
        def attributes(self):
            raise AssertionError("attributes read for a non-cs- room")

        @property
        def sid(self):
            raise AssertionError("sid read for a non-cs- room")

    ev = type(
        "Ev",
        (),
        {
            "event": "participant_joined",
            "room": Room(name="queue-_+886287654321_x"),
            "participant": Untouchable(),
        },
    )()
    assert livekit_dispatcher.handle_webhook_event(ev, None, None) is False


async def test_missing_dialed_number_goes_to_fallback(app, calls, monkeypatch):
    attrs = {k: v for k, v in SIP_ATTRS.items() if k != "sip.trunkPhoneNumber"}
    await _post(app, event=_event(attrs=attrs), monkeypatch=monkeypatch)
    assert calls["fallback"] == ["no_did"] and calls["launched"] == []


# --- dedup ------------------------------------------------------------------


async def test_redelivered_three_times_dispatches_once(app, calls, monkeypatch):
    for _ in range(3):
        r = await _post(app, event=_event(), monkeypatch=monkeypatch)
        assert r.status_code == 200
    assert calls["launched"] == [ROOM]
    assert calls["resolver"] == 1


async def test_concurrent_redelivery_dispatches_once(app, calls, monkeypatch):
    gate = asyncio.Event()

    async def slow_resolver(did):
        calls["resolver"] += 1
        await gate.wait()
        return (11, 22)

    monkeypatch.setattr(livekit_route, "_did_resolver", slow_resolver)
    monkeypatch.setattr(livekit_route, "_verify", lambda b, a: _event())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dograh-api:8000"
    ) as client:
        path = f"/api/v1/livekit/inbound/{SECRET}"
        r1, r2 = await asyncio.gather(
            client.post(path, content=b"{}"), client.post(path, content=b"{}")
        )
    assert r1.status_code == r2.status_code == 200  # acked while in flight
    gate.set()
    await _drain()
    assert calls["launched"] == [ROOM] and calls["resolver"] == 1


async def test_transient_resolver_failure_heals_without_5xx(app, calls, monkeypatch):
    calls["resolver_errors"] = 1
    r = await _post(app, event=_event(), monkeypatch=monkeypatch)
    assert r.status_code == 200
    assert calls["launched"] == [ROOM]
    assert calls["fallback"] == []


async def test_persistent_resolver_failure_goes_to_fallback(app, monkeypatch):
    """Real _dispatch: the resolver used to sit outside the try and 500 the
    webhook (review H2); now every exception reaches the fallback."""
    fallbacks = []

    async def resolver(did):
        raise ConnectionError("db down")

    async def fallback(room, reason, workflow_run_id=None):
        fallbacks.append((room, reason))

    monkeypatch.setattr(livekit_route, "_did_resolver", resolver)
    monkeypatch.setattr(livekit_route, "_fallback", fallback)
    r = await _post(app, event=_event(), monkeypatch=monkeypatch)
    assert r.status_code == 200
    assert fallbacks == [(ROOM, "dispatch_error")]
    # Handed off = committed: a redelivery does not REFER a second time.
    r = await _post(app, event=_event(), monkeypatch=monkeypatch)
    assert fallbacks == [(ROOM, "dispatch_error")]


async def test_dispatch_that_never_handed_off_releases_claim(monkeypatch):
    dedup = DispatchDedup()

    async def boom(*a, **kw):
        raise RuntimeError("dispatch broke")

    async def fallback_also_broken(room, reason, run_id=None):
        raise RuntimeError("safetynet spawn broke")

    monkeypatch.setattr(livekit_dispatcher, "_dispatch", boom)
    assert dedup.claim("SCL_1")
    with pytest.raises(RuntimeError):
        await livekit_dispatcher.dispatch_livekit_call(
            ROOM, SIP_ATTRS, None, fallback_also_broken, dedup_key="SCL_1", dedup=dedup
        )
    assert dedup.claim("SCL_1")  # next delivery is not swallowed


async def test_cancelled_dispatch_releases_claim(monkeypatch):
    dedup = DispatchDedup()
    started = asyncio.Event()

    async def hang(*a, **kw):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(livekit_dispatcher, "_dispatch", hang)
    assert dedup.claim("SCL_1")
    task = asyncio.create_task(
        livekit_dispatcher.dispatch_livekit_call(
            ROOM, SIP_ATTRS, None, None, dedup_key="SCL_1", dedup=dedup
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert dedup.claim("SCL_1")


async def test_dedup_covers_overflow_branch(monkeypatch):
    """Overflow hand-off commits too: a redelivery must not REFER twice."""
    dedup = DispatchDedup()

    async def overflow_dispatch(*a, **kw):
        return True  # _dispatch returns True after spawning capacity_overflow

    monkeypatch.setattr(livekit_dispatcher, "_dispatch", overflow_dispatch)
    assert dedup.claim("SCL_1")
    await livekit_dispatcher.dispatch_livekit_call(
        ROOM, SIP_ATTRS, None, None, dedup_key="SCL_1", dedup=dedup
    )
    assert not dedup.claim("SCL_1")


def test_new_events_reach_the_alert_channel():
    from api.services.observability.alerts import IMMEDIATE_EVENTS, WINDOWED_EVENTS
    from api.services.pipecat.livekit_safetynet import RECONCILE_FAILED_EVENT

    assert livekit_dispatcher.WEBHOOK_REJECTED_EVENT in IMMEDIATE_EVENTS
    assert RECONCILE_FAILED_EVENT in WINDOWED_EVENTS
