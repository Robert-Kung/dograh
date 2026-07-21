"""Safetynet tests (S-L3-SAFETYNET): dispatch-face fallback, mid-call fatal
transfer, silence watchdog, config validation, structured events."""

import asyncio
import types

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    ClientConnectedFrame,
    ErrorFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)

from api.services.pipecat import livekit_safetynet as sn
from api.services.pipecat.livekit_safetynet import (
    SafetynetWatchdog,
    claim,
    midcall_safetynet,
    release,
    server_side_safetynet,
    validate_safetynet_config,
)


@pytest.fixture(autouse=True)
def _reset_latch():
    sn._fired_runs.clear()
    sn._fired_order.clear()
    yield


@pytest.fixture
def events(monkeypatch):
    captured = []

    def fake_log_event(event, **fields):
        captured.append({"event": event, **fields})

    monkeypatch.setattr(sn, "log_event", fake_log_event)
    return captured


def _fake_lk(participants, *, raise_on_transfer=False):
    captured = {"deleted": [], "transfers": []}

    async def list_participants(req):
        return types.SimpleNamespace(participants=participants)

    async def transfer(req):
        if raise_on_transfer:
            raise RuntimeError("provider rejected")
        captured["transfers"].append(req.transfer_to)

    async def delete_room(req):
        captured["deleted"].append(req.room)

    return types.SimpleNamespace(
        room=types.SimpleNamespace(
            list_participants=list_participants, delete_room=delete_room
        ),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
    ), captured


def _sip_caller():
    from api.services.pipecat.livekit_cold_transfer import SIP_KIND

    return types.SimpleNamespace(kind=SIP_KIND, identity="sip_abc")


async def await_fallback(fallback_coro):
    """Await the route's _fallback and the background safetynet task it spawns."""
    task = await fallback_coro
    if task is not None:
        await task


# --- config validation (1.1) ---


def test_validate_config_unset_ok(monkeypatch):
    monkeypatch.delenv("SAFETYNET_FALLBACK_QUEUE", raising=False)
    monkeypatch.delenv("SAFETYNET_MAX_SILENCE_SECONDS", raising=False)
    validate_safetynet_config()


def test_validate_config_valid_queue_ok(monkeypatch):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    validate_safetynet_config()


def test_validate_config_bad_queue_fails(monkeypatch):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "0912345678")
    with pytest.raises(RuntimeError, match="SAFETYNET_FALLBACK_QUEUE"):
        validate_safetynet_config()


def test_validate_config_bad_seconds_fails(monkeypatch):
    monkeypatch.setenv("SAFETYNET_MAX_SILENCE_SECONDS", "eight")
    with pytest.raises(RuntimeError, match="SAFETYNET_MAX_SILENCE_SECONDS"):
        validate_safetynet_config()


def test_validate_config_nonpositive_seconds_fails(monkeypatch):
    monkeypatch.setenv("SAFETYNET_MAX_SILENCE_SECONDS", "0")
    with pytest.raises(RuntimeError, match="must be > 0"):
        validate_safetynet_config()


# --- once-per-run latch ---


def test_claim_is_sticky_per_run():
    assert claim(41)
    assert not claim(41)
    assert claim(42)


def test_claim_without_run_id_never_latches():
    # Pre-run dispatch failures repeat per DID (room names are cs-<DID>);
    # latching them would poison the phone number for later calls.
    assert claim(None)
    assert claim(None)


def test_release_reopens_claim():
    assert claim(41)
    release(41)
    assert claim(41)


def test_claim_evicts_oldest_beyond_cap():
    for i in range(sn._MAX_FIRED + 1):
        assert claim(i)
    assert claim(0)  # evicted, claimable again
    assert not claim(sn._MAX_FIRED)


# --- dispatch face (2.x) ---


@pytest.mark.asyncio
async def test_non_cs_room_ignored(monkeypatch, events):
    lk, cap = _fake_lk([_sip_caller()])
    await server_side_safetynet("playground-room", "no_did", lk=lk)
    assert cap["transfers"] == [] and cap["deleted"] == []
    assert events == []


@pytest.mark.asyncio
async def test_dispatch_refer_to_fallback_queue(monkeypatch, events):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    lk, cap = _fake_lk([_sip_caller()])
    await server_side_safetynet("cs-+886912", "unmapped_did", workflow_run_id=7, lk=lk)
    assert cap["transfers"] == ["tel:+886900000000"]
    assert cap["deleted"] == []
    assert [e["event"] for e in events] == [
        "safetynet.triggered",
        "safetynet.transfer_ok",
    ]
    assert events[0]["reason"] == "unmapped_did"
    assert events[0]["workflow_run_id"] == 7
    assert events[1]["elapsed_ms"] is not None


@pytest.mark.asyncio
async def test_dispatch_refer_failure_deletes_room(monkeypatch, events):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    lk, cap = _fake_lk([_sip_caller()], raise_on_transfer=True)
    await server_side_safetynet("cs-+886912", "launch_failed", lk=lk)
    assert cap["deleted"] == ["cs-+886912"]
    assert [e["event"] for e in events] == [
        "safetynet.triggered",
        "safetynet.transfer_failed",
        "safetynet.terminated",
    ]


@pytest.mark.asyncio
async def test_dispatch_no_sip_caller_deletes_room(monkeypatch, events):
    from api.services.pipecat import livekit_cold_transfer

    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    # exhaust the SIP-participant wait without 6×500ms of real sleeping
    monkeypatch.setattr(livekit_cold_transfer, "WAIT_SIP_INTERVAL_SECONDS", 0.0)
    web = types.SimpleNamespace(kind=0, identity="agent-1")
    lk, cap = _fake_lk([web])
    await server_side_safetynet("cs-+886912", "no_did", lk=lk)
    assert cap["transfers"] == []
    assert cap["deleted"] == ["cs-+886912"]
    assert events[-1]["event"] == "safetynet.terminated"


@pytest.mark.asyncio
async def test_dispatch_unconfigured_queue_deletes_room(monkeypatch, events):
    monkeypatch.delenv("SAFETYNET_FALLBACK_QUEUE", raising=False)
    lk, cap = _fake_lk([_sip_caller()])
    await server_side_safetynet("cs-+886912", "no_did", lk=lk)
    assert cap["transfers"] == []
    assert cap["deleted"] == ["cs-+886912"]


@pytest.mark.asyncio
async def test_dispatch_second_trigger_same_run_skipped(monkeypatch, events):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    lk, cap = _fake_lk([_sip_caller()])
    await server_side_safetynet("cs-+886912", "launch_failed", workflow_run_id=9, lk=lk)
    await server_side_safetynet(
        "cs-+886912", "pipeline_exception", workflow_run_id=9, lk=lk
    )
    assert cap["transfers"] == ["tel:+886900000000"]
    assert len([e for e in events if e["event"] == "safetynet.triggered"]) == 1


@pytest.mark.asyncio
async def test_dispatch_next_call_same_room_not_poisoned(monkeypatch, events):
    """Room names repeat per DID (cs-{call.to}): a fired safetynet for one call
    must not block the next call's safetynet on the same number."""
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    lk, cap = _fake_lk([_sip_caller()])
    await server_side_safetynet("cs-+886912", "launch_failed", workflow_run_id=1, lk=lk)
    await server_side_safetynet("cs-+886912", "launch_failed", workflow_run_id=2, lk=lk)
    assert len(cap["transfers"]) == 2


@pytest.mark.asyncio
async def test_launch_failed_via_done_callback(monkeypatch):
    """Real launch failures happen inside the fire-and-forget pipeline task;
    the done callback must route them to fallback with the run id (2.2)."""
    from api.services.pipecat import livekit_dispatcher, run_pipeline

    async def resolver(did):
        return (1, 2)

    async def fake_create_workflow_run(**kw):
        return types.SimpleNamespace(id=77)

    async def exploding_pipeline(**kw):
        raise RuntimeError("transport connect failed")

    fb = {}

    async def fallback(room, reason, workflow_run_id=None):
        fb.update(room=room, reason=reason, workflow_run_id=workflow_run_id)

    from api.db import db_client

    monkeypatch.setattr(db_client, "create_workflow_run", fake_create_workflow_run)
    monkeypatch.setattr(run_pipeline, "run_pipeline_livekit", exploding_pipeline)
    monkeypatch.setattr(livekit_dispatcher, "_sign_agent_token", lambda r, i: "jwt")
    monkeypatch.setenv("LIVEKIT_URL", "ws://test")

    await livekit_dispatcher.dispatch_livekit_call(
        "cs-+886912345678", resolver, fallback
    )
    for _ in range(10):  # let the task die and the callback's task run
        await asyncio.sleep(0)
    assert fb == {
        "room": "cs-+886912345678",
        "reason": "launch_failed",
        "workflow_run_id": 77,
    }


@pytest.mark.asyncio
async def test_prelaunch_failure_routes_to_fallback(monkeypatch):
    """DB/env/token failures before the task exists must also hit fallback,
    not escape to the webhook handler as a 500."""
    from api.services.pipecat import livekit_dispatcher

    async def resolver(did):
        return (1, 2)

    async def broken_create_workflow_run(**kw):
        raise RuntimeError("db blip")

    fb = {}

    async def fallback(room, reason, workflow_run_id=None):
        fb.update(reason=reason, workflow_run_id=workflow_run_id)

    from api.db import db_client

    monkeypatch.setattr(db_client, "create_workflow_run", broken_create_workflow_run)
    await livekit_dispatcher.dispatch_livekit_call(
        "cs-+886912345678", resolver, fallback
    )
    assert fb == {"reason": "launch_failed", "workflow_run_id": None}


@pytest.mark.asyncio
async def test_route_fallback_wired_to_safetynet(monkeypatch):
    from api.routes import livekit as livekit_route

    called = {}

    async def fake_safetynet(room, reason, workflow_run_id=None, lk=None):
        called.update(room=room, reason=reason, workflow_run_id=workflow_run_id)

    monkeypatch.setattr(sn, "server_side_safetynet", fake_safetynet)
    await await_fallback(livekit_route._fallback("cs-+886912", "no_did"))
    assert called == {"room": "cs-+886912", "reason": "no_did", "workflow_run_id": None}


@pytest.mark.asyncio
async def test_e2e_route_fallback_refers_caller(monkeypatch, events):
    """Route fallback → server-side safetynet → SIP REFER, no layer mocked in
    between (4.2): the caller lands on the fallback queue, not in silence."""
    from api.routes import livekit as livekit_route
    from api.services.pipecat import livekit_cold_transfer

    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "sip:queue@pbx.example")
    monkeypatch.setenv("LIVEKIT_URL", "ws://test")
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")
    captured = {}

    async def fake_transfer(room, destination, **kwargs):
        captured.update(room=room, destination=destination)
        return {"status": "success", "action": "transferred"}

    async def fake_wait(room, **kwargs):
        return "sip_abc"

    monkeypatch.setattr(livekit_cold_transfer, "cold_transfer_to_human", fake_transfer)
    monkeypatch.setattr(livekit_cold_transfer, "wait_for_sip_participant", fake_wait)
    await await_fallback(livekit_route._fallback("cs-+886912345678", "unmapped_did"))
    assert captured == {
        "room": "cs-+886912345678",
        "destination": "sip:queue@pbx.example",
    }
    assert [e["event"] for e in events] == [
        "safetynet.triggered",
        "safetynet.transfer_ok",
    ]


# --- mid-call face (3.x) ---


def _fake_engine():
    frames = []

    async def queue_frame(frame):
        frames.append(frame)

    async def end_call_with_reason(reason, abort_immediately=False):
        calls.append(reason)

    async def resolve_transfer_call_config():
        return {"destination": "tel:+886955555555"}

    calls = []
    engine = types.SimpleNamespace(
        task=types.SimpleNamespace(queue_frame=queue_frame),
        end_call_with_reason=end_call_with_reason,
        resolve_transfer_call_config=resolve_transfer_call_config,
    )
    return engine, frames, calls


def _capture_execute(monkeypatch, result):
    from api.services.pipecat import livekit_transfer_flow

    captured = {}

    async def fake_execute(engine, **kwargs):
        captured.update(kwargs)
        if kwargs.get("before_refer") is not None:
            await kwargs["before_refer"]()
        return result

    monkeypatch.setattr(livekit_transfer_flow, "execute_cold_transfer", fake_execute)
    return captured


@pytest.mark.asyncio
async def test_midcall_bypasses_business_hours(monkeypatch, events):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    captured = _capture_execute(
        monkeypatch, {"status": "success", "action": "transferred"}
    )
    engine, frames, _ = _fake_engine()
    await midcall_safetynet(
        engine, room_name="cs-+886912", reason="bot_silence", workflow_run_id=11
    )
    assert captured["schedule"] is None
    assert captured["destination"] == "tel:+886900000000"
    assert captured["transfer_reason"] == "safetynet"
    assert any(
        "轉接專員" in getattr(f, "text", "") for f in frames
    )  # pre-REFER announce
    assert [e["event"] for e in events] == [
        "safetynet.triggered",
        "safetynet.transfer_ok",
    ]


@pytest.mark.asyncio
async def test_midcall_destination_falls_back_to_workflow_config(monkeypatch, events):
    monkeypatch.delenv("SAFETYNET_FALLBACK_QUEUE", raising=False)
    captured = _capture_execute(
        monkeypatch, {"status": "success", "action": "transferred"}
    )
    engine, _, _ = _fake_engine()
    await midcall_safetynet(
        engine, room_name="cs-+886912", reason="fatal_error", workflow_run_id=11
    )
    assert captured["destination"] == "tel:+886955555555"


@pytest.mark.asyncio
async def test_midcall_failure_announces_and_ends(monkeypatch, events):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    _capture_execute(
        monkeypatch,
        {"status": "failed", "action": "transfer_failed", "reason": "sip_refer_error"},
    )
    engine, frames, calls = _fake_engine()
    await midcall_safetynet(
        engine, room_name="cs-+886912", reason="bot_silence", workflow_run_id=11
    )
    assert any("請稍後再撥" in getattr(f, "text", "") for f in frames)
    assert calls == ["pipeline_error"]
    assert [e["event"] for e in events] == [
        "safetynet.triggered",
        "safetynet.transfer_failed",
        "safetynet.terminated",
    ]


@pytest.mark.asyncio
async def test_midcall_yields_to_inflight_transfer(monkeypatch, events):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    _capture_execute(
        monkeypatch,
        {
            "status": "failed",
            "action": "transfer_failed",
            "reason": "already_transferring",
        },
    )
    engine, _, calls = _fake_engine()
    await midcall_safetynet(
        engine, room_name="cs-+886912", reason="fatal_error", workflow_run_id=11
    )
    assert calls == []  # the in-flight transfer owns the exit
    assert [e["event"] for e in events] == ["safetynet.triggered"]


@pytest.mark.asyncio
async def test_midcall_engine_exception_falls_back_to_server_side(monkeypatch, events):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    from api.services.pipecat import livekit_transfer_flow

    async def broken_execute(engine, **kwargs):
        raise RuntimeError("engine half dead")

    monkeypatch.setattr(livekit_transfer_flow, "execute_cold_transfer", broken_execute)

    called = {}

    async def fake_server_side(room, reason, workflow_run_id=None, lk=None):
        called.update(room=room, reason=reason, workflow_run_id=workflow_run_id)

    monkeypatch.setattr(sn, "server_side_safetynet", fake_server_side)
    engine, _, _ = _fake_engine()
    await midcall_safetynet(
        engine, room_name="cs-+886912", reason="bot_silence", workflow_run_id=11
    )
    # release() reopened the claim so the server-side path can take over
    assert called == {
        "room": "cs-+886912",
        "reason": "midcall_safetynet_error",
        "workflow_run_id": 11,
    }
    assert claim(11)


@pytest.mark.asyncio
async def test_midcall_second_trigger_skipped(monkeypatch, events):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    captured = _capture_execute(
        monkeypatch,
        {"status": "failed", "action": "transfer_failed", "reason": "sip_refer_error"},
    )
    engine, _, _ = _fake_engine()
    await midcall_safetynet(
        engine, room_name="cs-+886912", reason="bot_silence", workflow_run_id=11
    )
    captured.clear()
    await midcall_safetynet(
        engine, room_name="cs-+886912", reason="fatal_error", workflow_run_id=11
    )
    assert captured == {}  # sticky latch: no second transfer even after failure


# --- watchdog (3.2) ---


def _push(frame):
    return types.SimpleNamespace(frame=frame)


def _watchdog(t, fired):
    async def on_fatal(reason):
        fired.append(reason)

    return SafetynetWatchdog(
        on_fatal=on_fatal,
        threshold_seconds=8.0,
        poll_seconds=999,  # monitor loop idle; tests drive due() directly
        clock=lambda: t[0],
    )


@pytest.mark.asyncio
async def test_watchdog_fires_on_owed_reply_silence():
    t, fired = [0.0], []
    wd = _watchdog(t, fired)
    await wd.on_push_frame(_push(UserStoppedSpeakingFrame()))
    t[0] = 7.9
    assert not wd.due(t[0])
    t[0] = 8.1
    assert wd.due(t[0])
    wd._fire("bot_silence")
    await asyncio.sleep(0)
    assert fired == ["bot_silence"]
    await wd.stop()


@pytest.mark.asyncio
async def test_watchdog_disarms_on_bot_speech():
    t, fired = [0.0], []
    wd = _watchdog(t, fired)
    await wd.on_push_frame(_push(UserStoppedSpeakingFrame()))
    await wd.on_push_frame(_push(BotStartedSpeakingFrame()))
    t[0] = 100.0
    assert not wd.due(t[0])
    await wd.stop()


@pytest.mark.asyncio
async def test_watchdog_disarms_when_user_speaks_again():
    t, fired = [0.0], []
    wd = _watchdog(t, fired)
    await wd.on_push_frame(_push(UserStoppedSpeakingFrame()))
    await wd.on_push_frame(
        _push(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=0.0))
    )
    t[0] = 100.0
    assert not wd.due(t[0])
    await wd.stop()


@pytest.mark.asyncio
async def test_watchdog_arms_for_greeting_on_connect():
    t, fired = [0.0], []
    wd = _watchdog(t, fired)
    await wd.on_push_frame(_push(ClientConnectedFrame()))
    t[0] = 8.1
    assert wd.due(t[0])
    await wd.stop()


@pytest.mark.asyncio
async def test_watchdog_suspended_during_tool_call():
    t, fired = [0.0], []
    wd = _watchdog(t, fired)
    await wd.on_push_frame(_push(UserStoppedSpeakingFrame()))
    await wd.on_push_frame(
        _push(
            FunctionCallInProgressFrame(
                function_name="lookup_ticket", tool_call_id="t1", arguments={}
            )
        )
    )
    t[0] = 100.0  # a slow MCP lookup must not be judged fatal
    assert not wd.due(t[0])
    await wd.on_push_frame(
        _push(
            FunctionCallResultFrame(
                function_name="lookup_ticket",
                tool_call_id="t1",
                arguments={},
                result={},
            )
        )
    )
    t[0] = 107.9  # clock restarts from the tool result
    assert not wd.due(t[0])
    t[0] = 108.1
    assert wd.due(t[0])
    await wd.stop()


@pytest.mark.asyncio
async def test_watchdog_fatal_error_fires_immediately():
    t, fired = [0.0], []
    wd = _watchdog(t, fired)
    await wd.on_push_frame(_push(ErrorFrame("llm dead", fatal=True)))
    await asyncio.sleep(0)
    assert fired == ["fatal_error"]
    await wd.on_push_frame(_push(ErrorFrame("stt dead", fatal=True)))
    await asyncio.sleep(0)
    assert fired == ["fatal_error"]  # fires once
    await wd.stop()


@pytest.mark.asyncio
async def test_watchdog_fatal_error_does_not_block_frame_path():
    """on_push_frame is awaited inline on the frame path; the safetynet's
    announce + REFER I/O must run in a background task, not inline."""
    started = asyncio.Event()
    release_cb = asyncio.Event()

    async def slow_on_fatal(reason):
        started.set()
        await release_cb.wait()

    wd = SafetynetWatchdog(on_fatal=slow_on_fatal, threshold_seconds=8.0)
    await asyncio.wait_for(
        wd.on_push_frame(_push(ErrorFrame("llm dead", fatal=True))), timeout=1.0
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    release_cb.set()
    await wd.stop()


@pytest.mark.asyncio
async def test_watchdog_nonfatal_error_ignored():
    t, fired = [0.0], []
    wd = _watchdog(t, fired)
    await wd.on_push_frame(_push(ErrorFrame("transient", fatal=False)))
    await asyncio.sleep(0)
    assert fired == []
    await wd.stop()


@pytest.mark.asyncio
async def test_watchdog_monitor_loop_fires():
    fired = []

    async def on_fatal(reason):
        fired.append(reason)

    wd = SafetynetWatchdog(on_fatal=on_fatal, threshold_seconds=0.02, poll_seconds=0.01)
    await wd.on_push_frame(_push(UserStoppedSpeakingFrame()))
    await asyncio.sleep(0.1)
    assert fired == ["bot_silence"]
    await wd.stop()
