"""LiveKit call lifecycle wiring (livekit-event-wiring §1).

Two layers against the **real** pipecat ``LiveKitTransport`` (review M15) —
MockTransport declares ``on_client_*``, which is exactly how the LIVEKIT path
lost its opening and hangup handling while every unit test stayed green:

1. every event name dograh registers is in the transport's declared set and
   no "not registered" warning is logged;
2. driving the transport's own client callbacks invokes the handlers.

Plus the transfer race (設計 B) and the run-end room delete (設計 F).
"""

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from livekit import rtc
from loguru import logger
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
from pipecat.utils.enums import EndTaskReason

from api.services.pipecat.livekit_call_events import (
    AGENT_DISCONNECTED_REASON,
    CALLER_ABSENT_REASON,
    LIVEKIT_EVENT_NAMES,
    ROOM_DELETE_FAILED_EVENT,
    LiveKitCallEvents,
    delete_room_at_run_end,
)

SIP = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
STANDARD = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD


class FakeEngine:
    """Idempotent like PipecatEngine.end_call_with_reason (first caller wins)."""

    def __init__(self):
        self.ended: list[tuple[str, bool]] = []
        self._disposed = False
        self.set_node = AsyncMock()
        self.queue_node_opening = AsyncMock()
        self.workflow = types.SimpleNamespace(start_node_id="start")
        self._call_context_vars = {}
        self.cleanup = AsyncMock()

    def is_call_disposed(self):
        return self._disposed

    async def end_call_with_reason(self, reason, abort_immediately=False):
        if self._disposed:
            return
        self._disposed = True
        self.ended.append((reason, abort_immediately))


def _participant(sid: str, identity: str, kind) -> types.SimpleNamespace:
    return types.SimpleNamespace(sid=sid, identity=identity, kind=kind)


def _transport(*participants) -> LiveKitTransport:
    transport = LiveKitTransport(
        url="ws://livekit-server:7880",
        token="tok",
        room_name="cs-_+886212345678_abc",
        params=LiveKitParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    transport._client._room = types.SimpleNamespace(
        remote_participants={p.identity: p for p in participants}
    )
    return transport


async def _drain(transport) -> None:
    """Event handlers run as tasks (BaseObject._call_event_handler)."""
    for _ in range(5):
        tasks = [t for _, t in list(transport._event_tasks)]
        if not tasks:
            await asyncio.sleep(0)
            continue
        await asyncio.gather(*tasks)


@pytest.fixture
def warnings():
    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="WARNING")
    yield captured
    logger.remove(sink)


def _call_events(transport, engine, **kw):
    connected = AsyncMock()
    hangup = AsyncMock()
    events = LiveKitCallEvents(
        transport,
        engine,
        on_caller_connected=connected,
        on_caller_hangup=hangup,
        **kw,
    )
    events.register()
    return events, connected, hangup


# --- layer 1: names are declared ------------------------------------------


def test_every_registered_event_name_is_declared(warnings):
    transport = _transport()
    _call_events(transport, FakeEngine())
    for name in LIVEKIT_EVENT_NAMES:
        assert name in transport._event_handlers, name
        assert transport._event_handlers[name].handlers, name
    assert not [w for w in warnings if "not registered" in w]


def test_on_call_state_updated_is_not_used():
    # Declared but never emitted by the client: a handler there is dead code.
    assert "on_call_state_updated" not in LIVEKIT_EVENT_NAMES


def _fake_task():
    handlers = {}

    def event_handler(name):
        def deco(fn):
            handlers[name] = fn
            return fn

        return deco

    task = types.SimpleNamespace(event_handler=event_handler, turn_trace_observer=None)
    return task, handlers


def _register(transport, engine):
    from api.services.pipecat.event_handlers import register_event_handlers

    task, task_handlers = _fake_task()
    audio_buffer = MagicMock()
    audio_buffer.start_recording = AsyncMock()
    audio_buffer.stop_recording = AsyncMock()
    register_event_handlers(
        task,
        transport,
        workflow_run_id=1,
        engine=engine,
        audio_buffer=audio_buffer,
        in_memory_logs_buffer=MagicMock(),
        pipeline_metrics_aggregator=MagicMock(),
        audio_config=None,
    )
    return task_handlers, audio_buffer


def test_register_event_handlers_uses_only_declared_livekit_events(warnings):
    transport = _transport()
    _register(transport, FakeEngine())
    assert not [w for w in warnings if "not registered" in w]
    for name in LIVEKIT_EVENT_NAMES:
        assert transport._event_handlers[name].handlers, name


# --- layer 2: driven through the transport client --------------------------


@patch("api.services.pipecat.event_handlers._capture_call_event", new=AsyncMock())
async def test_caller_already_in_room_connects_once():
    caller = _participant("PA_sip", "sip_+886911000001", SIP)
    transport = _transport(caller)
    engine = FakeEngine()
    task_handlers, audio_buffer = _register(transport, engine)

    await task_handlers["on_pipeline_started"](None, None)
    # LiveKitTransportClient.connect(): on_connected, then the caller already
    # present fires on_first_participant_joined with participants[0].
    callbacks = transport._client._callbacks
    await callbacks.on_connected()
    await callbacks.on_first_participant_joined(transport._client.get_participants()[0])
    await _drain(transport)

    audio_buffer.start_recording.assert_awaited_once()
    engine.set_node.assert_awaited_once_with("start")
    engine.queue_node_opening.assert_awaited_once()

    # A later re-fire (room emptied and refilled) does not replay the opening.
    await callbacks.on_first_participant_joined("PA_sip")
    await _drain(transport)
    engine.set_node.assert_awaited_once()


@patch("api.services.pipecat.event_handlers._capture_call_event", new=AsyncMock())
async def test_sip_caller_leaving_ends_call_as_user_hangup():
    caller = _participant("PA_sip", "sip_+886911000001", SIP)
    transport = _transport(caller)
    engine = FakeEngine()
    _, audio_buffer = _register(transport, engine)
    client = transport._client

    await client._async_on_participant_connected(caller)
    await _drain(transport)
    await client._async_on_participant_disconnected(caller)
    await _drain(transport)

    audio_buffer.stop_recording.assert_awaited()
    assert engine.ended == [(EndTaskReason.USER_HANGUP.value, True)]


async def test_non_caller_leaving_does_not_end_call():
    caller = _participant("PA_sip", "sip_+886911000001", SIP)
    other = _participant("PA_sup", "supervisor-1", STANDARD)
    transport = _transport(caller, other)
    engine = FakeEngine()
    _, connected, hangup = _call_events(transport, engine)
    client = transport._client

    await client._async_on_participant_connected(caller)
    await client._async_on_participant_connected(other)
    await _drain(transport)
    await client._async_on_participant_disconnected(other)
    await _drain(transport)

    hangup.assert_not_awaited()
    assert engine.ended == []


async def test_first_participant_not_sip_disables_hangup_detection(warnings):
    other = _participant("PA_x", "someone", STANDARD)
    transport = _transport(other)
    engine = FakeEngine()
    events, connected, hangup = _call_events(transport, engine)

    await transport._client._async_on_participant_connected(other)
    await transport._client._async_on_participant_disconnected(other)
    await _drain(transport)

    connected.assert_awaited_once()
    hangup.assert_not_awaited()
    assert events.caller_sid is None
    assert [w for w in warnings if "not SIP" in w]


async def test_agent_disconnect_ends_call():
    transport = _transport()
    engine = FakeEngine()
    _call_events(transport, engine)

    await transport._client._callbacks.on_disconnected()
    await _drain(transport)

    assert engine.ended == [(AGENT_DISCONNECTED_REASON, True)]


async def test_agent_disconnect_after_call_ended_is_noop():
    transport = _transport()
    engine = FakeEngine()
    _call_events(transport, engine)
    await engine.end_call_with_reason("end_call_tool")

    await transport._client._callbacks.on_disconnected()
    await _drain(transport)

    assert engine.ended == [("end_call_tool", False)]


async def test_caller_absent_after_agent_joined_ends_call():
    transport = _transport()  # nobody in the room
    engine = FakeEngine()
    events, connected, _ = _call_events(transport, engine, caller_absent_seconds=0.05)

    await transport._client._callbacks.on_connected()
    await _drain(transport)
    await asyncio.wait_for(events._absent_task, 1)

    connected.assert_not_awaited()
    assert engine.ended == [(CALLER_ABSENT_REASON, True)]


async def test_caller_present_cancels_absent_end():
    caller = _participant("PA_sip", "sip_+886911000001", SIP)
    transport = _transport(caller)
    engine = FakeEngine()
    events, _, _ = _call_events(transport, engine, caller_absent_seconds=0.05)

    await transport._client._callbacks.on_connected()
    await transport._client._callbacks.on_first_participant_joined("PA_sip")
    await _drain(transport)
    await asyncio.wait_for(events._absent_task, 1)

    assert engine.ended == []


async def test_close_cancels_absent_timer():
    transport = _transport()
    engine = FakeEngine()
    events, _, _ = _call_events(transport, engine, caller_absent_seconds=5)

    await transport._client._callbacks.on_connected()
    await _drain(transport)
    events.close()
    await asyncio.sleep(0)

    assert events._absent_task.cancelled()
    assert engine.ended == []


# --- other transports keep the on_client_* events --------------------------


class _ClientEventTransport(BaseTransport):
    """Stand-in for SMALLWEBRTC/telephony: declares the on_client_* pair."""

    def __init__(self):
        super().__init__()
        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")

    def input(self):
        raise NotImplementedError

    def output(self):
        raise NotImplementedError


@patch("api.services.pipecat.event_handlers._capture_call_event", new=AsyncMock())
async def test_non_livekit_transport_keeps_client_events(warnings):
    transport = _ClientEventTransport()
    engine = FakeEngine()
    task_handlers, audio_buffer = _register(transport, engine)
    assert not [w for w in warnings if "not registered" in w]

    await task_handlers["on_pipeline_started"](None, None)
    await transport._call_event_handler("on_client_connected", "client")
    await _drain(transport)
    audio_buffer.start_recording.assert_awaited_once()
    engine.set_node.assert_awaited_once()

    await transport._call_event_handler("on_client_disconnected", "client")
    await _drain(transport)
    assert engine.ended == [(EndTaskReason.USER_HANGUP.value, True)]


# --- transfer race (設計 B) ------------------------------------------------


def _transfer_lk(*, on_transfer=None, raise_on_transfer=False, hang=False):
    from api.services.pipecat.livekit_cold_transfer import SIP_KIND

    async def list_participants(req):
        return types.SimpleNamespace(
            participants=[types.SimpleNamespace(kind=SIP_KIND, identity="sip_caller")]
        )

    async def transfer(req):
        if on_transfer is not None:
            await on_transfer()
        if hang:
            await asyncio.sleep(3600)
        if raise_on_transfer:
            raise RuntimeError("sip status: 486")

    return types.SimpleNamespace(
        room=types.SimpleNamespace(list_participants=list_participants),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
    )


class _TransferEngine(FakeEngine):
    def __init__(self):
        super().__init__()

        async def queue_frame(frame):
            pass

        self.task = types.SimpleNamespace(queue_frame=queue_frame)


@pytest.fixture
def no_db():
    with (
        patch(
            "api.services.observability.call_outcome.record_call_outcome",
            new=AsyncMock(),
        ) as outcome,
        patch(
            "api.services.pipecat.transfer_context_handoff.prepare_transfer_handoff",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield outcome


async def _connected_caller(engine):
    caller = _participant("PA_sip", "sip_caller", SIP)
    transport = _transport(caller)
    events, _, hangup = _call_events(transport, engine)
    await transport._client._async_on_participant_connected(caller)
    await _drain(transport)
    return transport, caller, hangup


async def test_caller_leg_leaving_before_transfer_settles_keeps_transferred(no_db):
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    engine = _TransferEngine()
    transport, caller, hangup = await _connected_caller(engine)

    async def caller_leg_leaves():
        # Real SIP: livekit-sip BYEs the caller leg once NOTIFY 200 lands,
        # before TransferSIPParticipant returns (task 0.3).
        await transport._client._async_on_participant_disconnected(caller)
        await _drain(transport)

    res = await execute_cold_transfer(
        engine,
        room_name="cs-_+886212345678_abc",
        destination="tel:+886287654321",
        lk=_transfer_lk(on_transfer=caller_leg_leaves),
        transfer_reason="voice_tool",
    )

    assert res["status"] == "success"
    hangup.assert_not_awaited()
    assert engine.ended == [(EndTaskReason.TRANSFER_CALL.value, False)]
    assert no_db.await_args.kwargs["outcome"] == "transferred:voice_tool"
    assert engine._livekit_transfer_in_progress is False


async def test_refer_failure_after_caller_left_ends_call_at_once(no_db):
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    engine = _TransferEngine()
    transport, caller, hangup = await _connected_caller(engine)

    async def caller_leaves():
        await transport._client._async_on_participant_disconnected(caller)
        await _drain(transport)

    res = await execute_cold_transfer(
        engine,
        room_name="cs-_+886212345678_abc",
        destination="tel:+886287654321",
        lk=_transfer_lk(on_transfer=caller_leaves, raise_on_transfer=True),
        transfer_reason="voice_tool",
    )

    assert res["status"] == "failed"
    hangup.assert_not_awaited()
    # Ended by the flow before the result reaches the LLM; keeps extraction.
    assert engine.ended == [(EndTaskReason.USER_HANGUP.value, False)]
    assert engine.is_call_disposed()


async def test_refer_failure_with_caller_present_leaves_call_to_llm(no_db):
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    engine = _TransferEngine()
    await _connected_caller(engine)

    res = await execute_cold_transfer(
        engine,
        room_name="cs-_+886212345678_abc",
        destination="tel:+886287654321",
        lk=_transfer_lk(raise_on_transfer=True),
        transfer_reason="voice_tool",
    )

    assert res["status"] == "failed"
    assert engine.ended == []


async def test_stuck_transfer_records_transfer_unknown(no_db):
    from api.services.pipecat import livekit_transfer_flow
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    engine = _TransferEngine()
    with patch.object(livekit_transfer_flow, "TRANSFER_SETTLE_TIMEOUT_SECONDS", 0.05):
        flow = asyncio.create_task(
            execute_cold_transfer(
                engine,
                room_name="cs-_+886212345678_abc",
                destination="tel:+886287654321",
                lk=_transfer_lk(hang=True),
                transfer_reason="voice_tool",
            )
        )
        for _ in range(100):
            if engine.ended:
                break
            await asyncio.sleep(0.02)
        flow.cancel()

    assert engine.ended == [(livekit_transfer_flow.TRANSFER_UNKNOWN_REASON, True)]
    assert no_db.await_args.kwargs["outcome"] == "transfer_unknown"


async def test_disposed_call_gets_no_llm_turn_after_failed_transfer():
    from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager

    engine = FakeEngine()
    await engine.end_call_with_reason(EndTaskReason.USER_HANGUP.value)
    manager = CustomToolManager.__new__(CustomToolManager)
    manager._engine = engine
    manager._handle_transfer_result = AsyncMock()
    params = types.SimpleNamespace(result_callback=AsyncMock())
    run = types.SimpleNamespace(id=1, initial_context={"room_name": "cs-x"})

    with patch(
        "api.services.workflow.pipecat_engine_custom_tools.execute_cold_transfer",
        new=AsyncMock(return_value={"status": "failed", "reason": "sip_refer_error"}),
    ):
        await manager._handle_livekit_cold_transfer(
            {"destination": "tel:+886287654321"}, run, params, None
        )

    manager._handle_transfer_result.assert_not_awaited()
    props = params.result_callback.await_args.kwargs["properties"]
    assert props.run_llm is False


# --- run-end room delete (設計 F) -------------------------------------------


async def test_room_deleted_at_run_end():
    deleted = []

    async def delete_room(req):
        deleted.append(req.room)

    lk = types.SimpleNamespace(room=types.SimpleNamespace(delete_room=delete_room))
    await delete_room_at_run_end("cs-_+886212345678_abc", 7, lk=lk)
    assert deleted == ["cs-_+886212345678_abc"]


async def test_room_delete_failure_emits_event():
    async def delete_room(req):
        raise RuntimeError("unreachable")

    lk = types.SimpleNamespace(room=types.SimpleNamespace(delete_room=delete_room))
    with patch("api.services.observability.call_events.emit") as emit:
        await delete_room_at_run_end("cs-x", 7, lk=lk)
    emit.assert_called_once()
    assert emit.call_args.args[0] == ROOM_DELETE_FAILED_EVENT
    assert emit.call_args.kwargs["workflow_run_id"] == 7


def test_room_delete_failure_alerts_immediately():
    from api.services.observability.alerts import IMMEDIATE_EVENTS

    assert ROOM_DELETE_FAILED_EVENT in IMMEDIATE_EVENTS


@pytest.mark.parametrize(
    "ending",
    ["completed", "pipeline_exception", "cancelled"],
)
async def test_run_pipeline_livekit_deletes_room_on_every_ending(ending):
    """AI end-call node / safetynet failure / idle timeout all return through
    run_pipeline_livekit; a crash goes through its except; all hit the finally."""
    from api.services.pipecat import run_pipeline

    async def impl(*a, **kw):
        if ending == "pipeline_exception":
            raise RuntimeError("boom")
        if ending == "cancelled":
            raise asyncio.CancelledError

    with (
        patch.object(run_pipeline, "_run_pipeline_livekit_impl", new=impl),
        patch.object(run_pipeline, "register_active_call"),
        patch.object(run_pipeline, "unregister_active_call"),
        patch(
            "api.services.pipecat.livekit_safetynet.server_side_safetynet",
            new=AsyncMock(),
        ),
        patch(
            "api.services.pipecat.livekit_call_events.delete_room_at_run_end",
            new=AsyncMock(),
        ) as delete,
    ):
        try:
            await run_pipeline.run_pipeline_livekit(
                "ws://lk", "tok", "cs-_+886212345678_abc", 1, 42, 1
            )
        except (RuntimeError, asyncio.CancelledError):
            pass

    delete.assert_awaited_once_with("cs-_+886212345678_abc", 42)


async def test_sip_caller_joining_after_a_non_sip_participant_is_tracked():
    """review D-10: the first participant need not be the caller."""
    other = _participant("PA_x", "someone", STANDARD)
    caller = _participant("PA_sip", "sip_+886911000001", SIP)
    transport = _transport(other, caller)
    engine = FakeEngine()
    events, _, hangup = _call_events(transport, engine)
    client = transport._client

    await client._async_on_participant_connected(other)
    await client._async_on_participant_connected(caller)
    await _drain(transport)
    assert events.caller_sid == "PA_sip"

    await client._async_on_participant_disconnected(caller)
    await _drain(transport)
    hangup.assert_awaited_once()
