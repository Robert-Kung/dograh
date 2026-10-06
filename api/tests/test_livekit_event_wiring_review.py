"""Regression tests for the livekit-event-wiring implementation review
(``openspec/changes/livekit-event-wiring/review-2026-10-06-impl.md``)."""

import asyncio
import time
import types
from unittest.mock import AsyncMock, patch

import pytest

from api.services.pipecat import livekit_dispatcher, livekit_safetynet
from api.services.pipecat.livekit_dispatcher import DispatchDedup

# --- D-02: the TW hint only applies to national format ---------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+15551234567", "+15551234567"),
        ("0015551234567", "+15551234567"),
        ("0212345678", "+886212345678"),
        ("+886212345678", "+886212345678"),
        ("212345678", "+886212345678"),  # national without trunk prefix (F13)
        ("886212345678", "+886212345678"),
        ("(02)1234-5678", "+886212345678"),
    ],
)
def test_did_hint_does_not_rewrite_foreign_numbers(raw, expected):
    attrs = {"sip.trunkPhoneNumber": raw}
    assert livekit_dispatcher.did_from_sip_attributes(attrs) == expected


# --- D-05: a hung dispatch is bounded and handed to the fallback ------------


async def test_hung_dispatch_times_out_to_fallback(monkeypatch):
    async def hanging_resolver(did):
        await asyncio.sleep(3600)

    fallbacks = []

    async def fallback(room, reason, run_id=None):
        await asyncio.sleep(0.1)  # longer than the budget: must not be cut (F5)
        fallbacks.append(reason)

    monkeypatch.setattr(livekit_dispatcher, "DISPATCH_TIMEOUT_SECONDS", 0.05)
    dedup = DispatchDedup()
    assert dedup.claim("SCL_1")
    await livekit_dispatcher.dispatch_livekit_call(
        "cs-x",
        {"sip.trunkPhoneNumber": "+886212345678"},
        hanging_resolver,
        fallback,
        dedup_key="SCL_1",
        dedup=dedup,
    )
    assert fallbacks == ["dispatch_error"]
    assert not dedup.claim("SCL_1")  # handed off = committed


# --- D-01: the transfer watchdog's ending survives its own cancellation ------


async def test_transfer_unknown_ending_survives_watchdog_cancel(monkeypatch):
    from api.services.pipecat import livekit_transfer_flow as flow

    monkeypatch.setattr(flow, "TRANSFER_SETTLE_TIMEOUT_SECONDS", 0)
    pushed = []

    class Engine:
        _disposed = False

        def is_call_disposed(self):
            return self._disposed

        async def end_call_with_reason(self, reason, abort_immediately=False):
            self._disposed = True
            await asyncio.sleep(0.1)  # final extraction
            pushed.append((reason, abort_immediately))

    engine = Engine()
    with patch(
        "api.services.observability.call_outcome.record_call_outcome", new=AsyncMock()
    ):
        task = asyncio.create_task(
            flow._transfer_settle_watchdog(engine, "cs-x", "voice_tool")
        )
        for _ in range(50):
            if engine._disposed:
                break
            await asyncio.sleep(0.005)
        task.cancel()  # the flow returned mid-extraction
        await asyncio.sleep(0.2)
    assert pushed == [(flow.TRANSFER_UNKNOWN_REASON, True)]


async def test_settle_failure_still_clears_transfer_flag(monkeypatch):
    from api.services.pipecat import livekit_transfer_flow as flow

    async def boom(engine):
        raise RuntimeError("settle broke")

    monkeypatch.setattr(flow, "_settle_after_transfer", boom)
    engine = types.SimpleNamespace(
        task=types.SimpleNamespace(queue_frame=AsyncMock()),
        end_call_with_reason=AsyncMock(),
    )
    res = await flow.execute_cold_transfer(
        engine,
        room_name="cs-x",
        destination="tel:+886287654321",
        schedule={"tz": "Asia/Taipei"},
        after_hours_action="back_to_ai",
        now=__import__("datetime").datetime(
            2026, 6, 29, 2, 0, tzinfo=__import__("datetime").timezone.utc
        ),
    )
    assert res["status"] == "after_hours"
    assert engine._livekit_transfer_in_progress is False


# --- D-03: deleting an already-deleted room is not an alert ------------------


async def test_room_already_deleted_is_not_an_alert():
    from livekit.api.twirp_client import TwirpError

    from api.services.pipecat.livekit_call_events import delete_room_at_run_end

    async def delete_room(req):
        raise TwirpError("not_found", "room not found", status=404)

    lk = types.SimpleNamespace(room=types.SimpleNamespace(delete_room=delete_room))
    with patch("api.services.observability.call_events.emit") as emit:
        await delete_room_at_run_end("cs-x", 1, lk=lk)
    emit.assert_not_called()


# --- D-04 / D-06: reconciler isolates room errors and runs hand-offs concurrently


NOW = 1_791_250_000.0


def _lk(rooms, broken=()):
    async def list_rooms(req):
        return types.SimpleNamespace(
            rooms=[types.SimpleNamespace(name=n) for n in rooms]
        )

    async def list_participants(req):
        if req.room in broken:
            raise RuntimeError("room vanished")
        return types.SimpleNamespace(
            participants=[
                types.SimpleNamespace(
                    identity="sip_x",
                    kind=3,
                    joined_at=NOW - 30,
                    attributes={"sip.callID": f"SCL_{req.room}"},
                    sid=f"PA_{req.room}",
                )
            ]
        )

    return types.SimpleNamespace(
        room=types.SimpleNamespace(
            list_rooms=list_rooms, list_participants=list_participants
        )
    )


async def test_reconcile_skips_a_broken_room_and_continues(monkeypatch):
    handed = []

    async def fake(room, reason, run_id=None, lk=None):
        handed.append(room)

    monkeypatch.setattr(livekit_safetynet, "server_side_safetynet", fake)
    monkeypatch.setattr(livekit_safetynet.call_events, "emit", lambda *a, **k: None)
    n = await livekit_safetynet.reconcile_undispatched_rooms(
        _lk(["cs-a", "cs-b", "cs-c"], broken={"cs-a"}), now=NOW, dedup=DispatchDedup()
    )
    assert n == 2 and sorted(handed) == ["cs-b", "cs-c"]


async def test_reconcile_hand_offs_run_concurrently(monkeypatch):
    async def slow(room, reason, run_id=None, lk=None):
        await asyncio.sleep(0.2)

    monkeypatch.setattr(livekit_safetynet, "server_side_safetynet", slow)
    started = time.monotonic()
    n = await livekit_safetynet.reconcile_undispatched_rooms(
        _lk([f"cs-{i}" for i in range(5)]), now=NOW, dedup=DispatchDedup()
    )
    assert n == 5
    assert time.monotonic() - started < 0.6


# --- D-07: port parsing -------------------------------------------------------


@pytest.mark.parametrize(
    "argv, port",
    [
        (["uvicorn", "api.app:app", "--port", "8001"], "8001"),
        (["uvicorn", "api.app:app", "--port=8000"], "8000"),
        (["pytest"], None),
    ],
)
def test_uvicorn_port_parsing(argv, port):
    assert livekit_safetynet._uvicorn_port(argv) == port


# --- D-09: the caller left during the safetynet transfer ----------------------


async def test_midcall_safetynet_quiet_when_caller_left_during_transfer(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        livekit_safetynet, "log_event", lambda event, **kw: emitted.append(event)
    )
    monkeypatch.setattr(
        livekit_safetynet, "fallback_queue", lambda: "tel:+886287654321"
    )

    class Engine:
        def __init__(self):
            self.task = types.SimpleNamespace(queue_frame=AsyncMock())
            self.end_call_with_reason = AsyncMock()

        _livekit_caller_left = True  # the hangup handler deferred to the flow

        def is_call_disposed(self):
            return True

    async def failed_transfer(engine, **kw):
        return {"status": "failed", "reason": "no_sip_caller"}

    monkeypatch.setattr(
        "api.services.pipecat.livekit_transfer_flow.execute_cold_transfer",
        failed_transfer,
    )
    engine = Engine()
    await livekit_safetynet.midcall_safetynet(
        engine, room_name="cs-x", reason="bot_silence", workflow_run_id=987654
    )
    assert "safetynet.terminated" not in emitted
    assert "safetynet.transfer_failed" not in emitted
    engine.end_call_with_reason.assert_not_awaited()
