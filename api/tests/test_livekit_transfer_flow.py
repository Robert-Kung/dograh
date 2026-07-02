"""Shared cold-transfer preamble tests (S-L3-PRESS0 §3.6).

The planner suite is pure and runs anywhere. The executor suite exercises the
idempotency guard, REFER paths, and after-hours dispatch against a fake engine
and a fake LiveKitAPI; it needs the pipecat runtime (TTSSpeakFrame) so it is
skipped where pipecat is not installed (it runs in CI/Docker).
"""

import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from api.services.pipecat.livekit_transfer_flow import (
    TransferDecision,
    plan_transfer,
    valid_destination,
)

TPE = ZoneInfo("Asia/Taipei")
SCHED = {"tz": "Asia/Taipei", "mon": [["09:00", "18:00"]]}
OPEN = datetime(2026, 6, 29, 10, 0, tzinfo=TPE)  # Monday in hours
CLOSED = datetime(2026, 6, 29, 20, 0, tzinfo=TPE)  # Monday after hours

try:
    import pipecat.frames.frames  # noqa: F401

    PIPECAT = True
except ImportError:
    PIPECAT = False


# --- planner (pure) -------------------------------------------------------

def test_open_hours_always_refer():
    assert plan_transfer(SCHED, "announce_and_hangup", OPEN) is TransferDecision.REFER


def test_no_schedule_refers_failopen():
    assert plan_transfer(None, "announce_and_hangup", CLOSED) is TransferDecision.REFER


def test_after_hours_back_to_ai():
    assert plan_transfer(SCHED, "back_to_ai", CLOSED) is TransferDecision.AFTER_HOURS_BACK_TO_AI


def test_after_hours_hangup():
    assert plan_transfer(SCHED, "announce_and_hangup", CLOSED) is TransferDecision.AFTER_HOURS_HANGUP


def test_after_hours_alternate():
    assert plan_transfer(SCHED, "alternate_queue", CLOSED) is TransferDecision.AFTER_HOURS_ALTERNATE


def test_after_hours_unknown_falls_back_to_default():
    assert plan_transfer(SCHED, "carrier_pigeon", CLOSED) is TransferDecision.AFTER_HOURS_BACK_TO_AI


def test_after_hours_unset_falls_back_to_default():
    assert plan_transfer(SCHED, None, CLOSED) is TransferDecision.AFTER_HOURS_BACK_TO_AI


def test_valid_destination():
    assert valid_destination("tel:+886912345678")
    assert valid_destination("sip:queue@pbx.example")
    assert not valid_destination("0912345678")  # caller-shaped, not config-shaped
    assert not valid_destination("")
    assert not valid_destination(None)


# --- executor (needs pipecat) ---------------------------------------------

def _fake_engine():
    frames = []
    ended = []

    async def queue_frame(frame):
        frames.append(frame)

    async def end_call_with_reason(reason, abort_immediately=False):
        ended.append(reason)

    eng = types.SimpleNamespace(
        task=types.SimpleNamespace(queue_frame=queue_frame),
        end_call_with_reason=end_call_with_reason,
        _frames=frames,
        _ended=ended,
    )
    return eng


def _fake_lk(*, sip_caller=True, raise_on_transfer=False):
    from api.services.pipecat.livekit_cold_transfer import SIP_KIND

    captured = {}
    participants = (
        [types.SimpleNamespace(kind=SIP_KIND, identity="sip_caller")] if sip_caller else []
    )

    async def list_participants(req):
        return types.SimpleNamespace(participants=participants)

    async def transfer(req):
        if raise_on_transfer:
            raise RuntimeError("provider rejected")
        captured["transfer_to"] = req.transfer_to

    async def aclose():
        pass

    lk = types.SimpleNamespace(
        room=types.SimpleNamespace(list_participants=list_participants),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
        aclose=aclose,
    )
    return lk, captured


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_open_refer_success_ends_call_and_runs_before_refer():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    lk, cap = _fake_lk()
    announced = []

    async def before_refer():
        announced.append(True)

    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886912345678",
        schedule=SCHED,
        before_refer=before_refer,
        now=OPEN,
        lk=lk,
    )
    assert res["status"] == "success"
    assert announced == [True]
    assert cap["transfer_to"] == "tel:+886912345678"
    assert eng._ended  # call ended on success
    assert eng._livekit_transfer_in_progress is False  # flag cleared


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_refer_failure_no_retry_no_end_call():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    lk, _ = _fake_lk(raise_on_transfer=True)
    res = await execute_cold_transfer(
        eng, room_name="room1", destination="tel:+886912345678", schedule=SCHED, now=OPEN, lk=lk
    )
    assert res["status"] == "failed"
    assert res["reason"] == "sip_refer_error"
    assert not eng._ended  # not ended; LLM informs and offers fallback
    assert eng._livekit_transfer_in_progress is False


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_invalid_destination_rejected():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    res = await execute_cold_transfer(eng, room_name="room1", destination="0912", now=OPEN)
    assert res["reason"] == "invalid_destination"


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_idempotency_rejects_second_trigger():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    eng._livekit_transfer_in_progress = True  # already transferring
    lk, _ = _fake_lk()
    res = await execute_cold_transfer(
        eng, room_name="room1", destination="tel:+886912345678", schedule=SCHED, now=OPEN, lk=lk
    )
    assert res["reason"] == "already_transferring"


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_after_hours_back_to_ai_announces_no_transfer():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886912345678",
        schedule=SCHED,
        after_hours_action="back_to_ai",
        now=CLOSED,
    )
    assert res == {"status": "after_hours", "action": "back_to_ai"}
    assert eng._frames  # announcement queued
    assert not eng._ended


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_after_hours_hangup_announces_then_ends():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886912345678",
        schedule=SCHED,
        after_hours_action="announce_and_hangup",
        now=CLOSED,
    )
    assert res["action"] == "announced_hangup"
    assert eng._frames and eng._ended


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_after_hours_alternate_queue_refers_to_alternate():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    lk, cap = _fake_lk()
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886900000000",
        schedule=SCHED,
        after_hours_action="alternate_queue",
        alternate_destination="sip:night@pbx.example",
        now=CLOSED,
        lk=lk,
    )
    assert res["status"] == "success"
    assert cap["transfer_to"] == "sip:night@pbx.example"  # alternate, not primary


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_after_hours_alternate_without_target_falls_back_to_ai():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886900000000",
        schedule=SCHED,
        after_hours_action="alternate_queue",
        alternate_destination=None,
        now=CLOSED,
    )
    assert res == {"status": "after_hours", "action": "back_to_ai"}
    assert eng._frames and not eng._ended
