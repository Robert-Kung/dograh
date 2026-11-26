"""The LIVEKIT idle hangup says goodbye before the BYE (C4; idle-hangup-farewell).

Pipeline-level for the most part: the real ``LLMContextAggregatorPair`` and its
idle timer, the real engine, safetynet watchdog and LLM gates, a mock LLM and
mock TTS (``api/tests/support/idle_pipeline.py``). The caller never speaks
unless the test makes them, so the idle timer fires twice; the second time
ends the call.

The branches that need the call to be in a state the pipeline cannot be put in
from outside (a transfer in flight, the call already ending) run
``end_idle_call`` against the fake engine of ``livekit_failure_fakes``, still
with the real watchdog.
"""

import asyncio
import time
import types
from unittest.mock import AsyncMock, patch

import pytest
from google.genai import errors as genai_errors
from pipecat.frames.frames import ErrorFrame, TTSSpeakFrame, UserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.enums import EndTaskReason

from api.services.pipecat import livekit_idle_farewell as farewell
from api.services.pipecat import livekit_safetynet as sn
from api.services.pipecat.livekit_failure_exits import (
    IDLE_FAREWELL_TEXT,
    PLAYBACK_GAP_TIMEOUT_SECONDS,
    PLAYBACK_START_TIMEOUT_SECONDS,
)
from api.tests.support.idle_pipeline import (
    GREETING,
    IDLE_PROMPT,
    LLM_FAREWELL,
    ROOM,
    RUN,
    ScriptedTTS,
    build_idle_call,
    livekit_run,
    other_mode_run,
    run_idle_call,
    scripted_llm,
    seen,
)
from api.tests.support.livekit_failure_fakes import (
    FALLBACK,
    make_call,
    push_data,
    ready_call,
    settle,
    tts_error,
    until,
)
from api.tests.support.silence_budget import SILENCE_BUDGET, idle_hangup_silence

pytestmark = pytest.mark.usefixtures(
    "reset_safetynet_latch", "no_leaked_background_tasks"
)

IDLE = EndTaskReason.USER_IDLE_MAX_DURATION_EXCEEDED.value
UNPLAYED = farewell.IDLE_FAREWELL_UNPLAYED_EVENT
END_FORCED = sn.SAFETYNET_END_FORCED_EVENT
FAREWELL_MARK = IDLE_FAREWELL_TEXT[:8]

# Scaled-down bounds for the cases that run into them.
START_TIMEOUT = 0.5
GAP_TIMEOUT = 0.4
END_DEADLINE = 0.5
# Scheduling slack when comparing against a bound.
SLACK = 0.05


@pytest.fixture(autouse=True)
def _no_db():
    with patch(
        "api.db:db_client.get_organization_id_by_workflow_run_id",
        new_callable=AsyncMock,
        return_value=1,
    ):
        yield


@pytest.fixture
def exits(monkeypatch):
    """Record the call's server-side exits instead of reaching LiveKit."""
    from api.services.pipecat import livekit_call_events, livekit_cold_transfer

    seen_ = types.SimpleNamespace(events=[], refers=[], deleted=[])

    def log_event(event, **fields):
        seen_.events.append((event, fields))

    async def cold_transfer(room_name, destination, **kwargs):
        seen_.refers.append((room_name, destination))
        return {"status": "success"}

    async def delete_room(room_name, workflow_run_id, lk=None):
        seen_.deleted.append(room_name)

    async def record_call_fact(workflow_run_id, **facts):
        pass

    async def record_call_outcome(*args, **kwargs):
        pass

    def named(event):
        return [fields for name, fields in seen_.events if name == event]

    seen_.named = named
    monkeypatch.setattr(sn, "log_event", log_event)
    monkeypatch.setattr(sn, "record_call_fact", record_call_fact)
    monkeypatch.setattr(sn, "record_call_outcome", record_call_outcome)
    monkeypatch.setattr(livekit_cold_transfer, "cold_transfer_to_human", cold_transfer)
    monkeypatch.setattr(livekit_call_events, "delete_room_at_run_end", delete_room)
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)
    return seen_


@pytest.fixture
def plain_workflow(three_node_workflow_no_variable_extraction):
    return three_node_workflow_no_variable_extraction


@pytest.fixture
def extracting_workflow(three_node_workflow):
    return three_node_workflow


def _scale(call, monkeypatch):
    """Short playback and end bounds, for the cases that wait them out."""
    call.failure_exits.state.playback_start_timeout = START_TIMEOUT
    call.failure_exits.state.playback_gap_timeout = GAP_TIMEOUT
    monkeypatch.setattr(farewell, "IDLE_END_DEADLINE_SECONDS", END_DEADLINE)


def _farewell_tts(**kwargs):
    return ScriptedTTS(marker=FAREWELL_MARK, **kwargs)


# --- AC1 (tasks 1.1b): a third utterance reaches the caller before the end ---


@pytest.mark.parametrize("llm_delay", [0.0, 1.5], ids=["llm-instant", "llm-1.5s"])
@pytest.mark.parametrize(
    "extraction", [False, True], ids=["no-extraction", "extraction"]
)
async def test_silent_caller_hears_a_farewell_before_the_call_ends(
    request, exits, llm_delay, extraction
):
    """Greeting, idle prompt, farewell: three utterances reach the output
    transport before the EndFrame does, whatever the LLM's latency and the
    node's extraction setting. Red on the upstream idle handler for both
    no-extraction variants: the EndFrame overtakes the request for a goodbye."""
    workflow = request.getfixturevalue(
        "three_node_workflow"
        if extraction
        else "three_node_workflow_no_variable_extraction"
    )
    llm = scripted_llm(
        GREETING,
        IDLE_PROMPT,
        LLM_FAREWELL,
        response_delay=llm_delay,
        inference_delay=llm_delay,
    )
    call = await build_idle_call(workflow, llm)

    recording = await run_idle_call(call)

    assert recording.ended
    segments = recording.segments_before_end()
    assert len(segments) == 3, f"utterances before the end: {segments}"
    assert all(size > 0 for size in segments)
    assert recording.audio_after_end() == 0
    # One hangup: an idle timer firing again while a slow extraction holds
    # the end back does not start another.
    assert len(recording.kinds("end_call")) == 1
    assert exits.refers == []


async def test_the_farewell_is_fixed_text_and_the_call_ends_after_it(
    exits, plain_workflow
):
    llm = scripted_llm(GREETING, IDLE_PROMPT, LLM_FAREWELL)
    call = await build_idle_call(plain_workflow, llm)

    recording = await run_idle_call(call)

    # The LLM was asked for the idle prompt only; the goodbye is not its.
    assert llm.get_current_step() == 2
    assert recording.kinds("llm_asked", "speak", "end_call", "end") == [
        "llm_asked",
        "speak",
        "end_call",
        "end",
    ]
    assert recording.first("speak")[2] == IDLE_FAREWELL_TEXT
    assert call.tts.received_texts[-1] == IDLE_FAREWELL_TEXT
    # Ended only once the transport reported the goodbye over.
    assert recording.first("end_call")[1] >= recording.times("bot_stopped")[2]
    assert recording.first("end_call")[2] == IDLE
    assert call.engine._gathered_context["call_disposition"] == IDLE
    assert exits.events == []
    assert exits.deleted == []


# --- AC5 (tasks 1.2, 2.2): calls outside the scope keep upstream's idle ------


@pytest.mark.parametrize(
    "run, is_realtime",
    [
        (other_mode_run(), False),
        (livekit_run(), True),
        (livekit_run(room_name=None), False),
    ],
    ids=["not-livekit", "livekit-realtime", "livekit-no-watchdog"],
)
async def test_calls_out_of_scope_keep_the_upstream_idle_hangup(
    exits, plain_workflow, run, is_realtime
):
    """The second idle asks the LLM for a goodbye and ends the call right
    after, in that order, as before; no fixed goodbye is queued."""
    llm = scripted_llm(GREETING, IDLE_PROMPT, LLM_FAREWELL)
    call = await build_idle_call(
        plain_workflow, llm, workflow_run=run, is_realtime=is_realtime
    )

    recording = await run_idle_call(call)

    assert recording.kinds("idle", "llm_asked", "speak", "end_call") == [
        "idle",
        "llm_asked",
        "idle",
        "llm_asked",
        "end_call",
    ]
    assert recording.first("end_call")[2] == IDLE
    assert exits.named(UNPLAYED) == []


# --- tasks 2.1: the watchdog stands down during the idle hangup --------------


def _stt_error(services):
    exc = genai_errors.APIError(1011, {"error": {"message": "stream closed"}}, None)
    return ErrorFrame(error=f"STT: {exc}", exception=exc, processor=services.stt)


def _llm_error(services):
    exc = genai_errors.ClientError(
        429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}, None
    )
    return ErrorFrame(error=f"LLM: {exc}", exception=exc, processor=services.llm)


async def _trigger(call, services, kind):
    """Give the watchdog what makes it transfer on a call that is not ending."""
    if kind == "tts_error":
        await call.wd.on_push_frame(push_data(tts_error(services)))
    elif kind == "llm_error":
        await call.wd.on_push_frame(push_data(_llm_error(services)))
    elif kind == "stt_errors":
        for _ in range(3):
            await call.wd.on_push_frame(push_data(_stt_error(services)))
    elif kind == "bot_silence":
        call.wd._clock = time.monotonic
        call.wd._threshold = 0.05
        await call.wd.on_push_frame(push_data(UserStoppedSpeakingFrame()))


@pytest.mark.parametrize(
    "kind", ["tts_error", "llm_error", "stt_errors", "bot_silence"]
)
async def test_no_safetynet_transfer_while_the_idle_hangup_runs(services, refer, kind):
    call = await ready_call(make_call(services, refer))
    call.state.idle_end_in_progress = True

    await _trigger(call, services, kind)
    await settle(0.2)

    assert call.engine.entries("refer") == []
    assert call.engine.log == []  # no forced end spawned in its place either
    assert not call.wd._fired  # the trigger was dropped, not spent
    if kind == "tts_error":
        assert call.state.tts_unavailable  # the goodbye's wait still hears of it

    # Control: the same trigger transfers once the idle hangup is called off.
    call.state.idle_end_in_progress = False
    call.state.tts_unavailable = False
    await _trigger(call, services, kind)
    await until(lambda: call.engine.entries("refer"))
    await call.wd.stop()


# --- tasks 2.3: the call waits for the goodbye, the deadline does not --------


async def test_a_stuck_tts_is_waited_for_before_the_call_ends(
    exits, plain_workflow, monkeypatch
):
    """The goodbye never starts: the call ends only once the start timeout
    says so, reports the silent hangup, and — its EndFrame queued behind the
    same stuck TTS — is ended by the deadline."""
    call = await build_idle_call(
        plain_workflow,
        scripted_llm(GREETING, IDLE_PROMPT),
        tts=_farewell_tts(mode="hang"),
    )
    _scale(call, monkeypatch)

    recording = await run_idle_call(call)

    queued, decided = recording.first("speak")[1], recording.first("end_call")[1]
    assert decided - queued >= START_TIMEOUT - SLACK
    [unplayed] = exits.named(UNPLAYED)
    assert unplayed["reason"] == "start_timeout"
    assert unplayed["room_name"] == ROOM
    assert unplayed["workflow_run_id"] == RUN
    assert unplayed["elapsed_ms"] >= (START_TIMEOUT - SLACK) * 1000
    [forced] = exits.named(END_FORCED)
    assert forced["reason"] == "idle_end_stalled"
    assert exits.deleted == [ROOM]
    assert recording.end_time - decided <= END_DEADLINE + 1.0
    assert len(recording.segments_before_end()) == 2
    assert exits.refers == []
    assert call.engine._gathered_context["call_disposition"] == IDLE


async def test_a_slow_healthy_tts_is_not_cut_by_the_end_deadline(exits, plain_workflow):
    """First audio after 2.5 s and a 4 s goodbye: longer than the end deadline
    together, and played in full — the deadline counts from the decision to
    end, not from the idle event. Real bounds, no scaling."""
    goodbye_ms = 4000
    assert 2.5 < PLAYBACK_START_TIMEOUT_SECONDS
    assert 2.5 + goodbye_ms / 1000 > farewell.IDLE_END_DEADLINE_SECONDS
    call = await build_idle_call(
        plain_workflow,
        scripted_llm(GREETING, IDLE_PROMPT),
        tts=_farewell_tts(first_audio_delay=2.5, duration_ms=goodbye_ms),
    )

    recording = await run_idle_call(call, timeout=30)

    segments = recording.segments_before_end()
    assert len(segments) == 3
    assert segments[2] == goodbye_ms * 32  # 16 kHz, 16-bit: every byte went out
    assert recording.kinds("end", "cancel") == ["end"]
    assert exits.events == []


# --- tasks 2.4: the end is bounded from the decision to end ------------------


@pytest.mark.parametrize("tts_mode", ["play", "hang"], ids=["played", "unplayed"])
async def test_a_stuck_final_extraction_does_not_hold_the_call(
    exits, extracting_workflow, monkeypatch, tts_mode
):
    """The node extracts variables and the LLM never answers:
    ``end_call_with_reason`` would wait on it for good. Past the deadline the
    end task is cancelled, the disposition written, the room deleted and the
    pipeline cancelled — and the worker returns, which it would not while the
    idle handler was still waiting."""
    llm = scripted_llm(GREETING, IDLE_PROMPT, inference_delay=None)
    call = await build_idle_call(
        extracting_workflow, llm, tts=_farewell_tts(mode=tts_mode)
    )
    _scale(call, monkeypatch)

    recording = await run_idle_call(call)  # returning at all is the worker ending

    assert llm.inference_calls == 1  # the final extraction started, and hung
    assert recording.kinds("end", "cancel") == ["cancel"]  # no EndFrame was queued
    decided = recording.first("end_call")[1]
    assert END_DEADLINE - SLACK <= recording.end_time - decided <= END_DEADLINE + 1.0
    [forced] = exits.named(END_FORCED)
    assert forced == {
        "room_name": ROOM,
        "reason": "idle_end_stalled",
        "workflow_run_id": RUN,
    }
    assert exits.deleted == [ROOM]
    context = call.engine._gathered_context
    assert context["call_disposition"] == IDLE
    assert context["mapped_call_disposition"] == IDLE
    assert IDLE in context["call_tags"]
    assert len(exits.named(UNPLAYED)) == (1 if tts_mode == "hang" else 0)
    assert exits.refers == []


# --- tasks 2.5: a goodbye that did not play is reported, never transferred ---


async def test_playback_that_stalls_midway_is_reported(
    exits, plain_workflow, monkeypatch
):
    """The transport stops taking audio mid-goodbye: no heartbeat for the gap
    timeout, and it never reports the end."""
    call = await build_idle_call(
        plain_workflow,
        scripted_llm(GREETING, IDLE_PROMPT),
        tts=_farewell_tts(duration_ms=2000),
    )
    _scale(call, monkeypatch)

    async def transport_stalls(call):
        await seen(call, "speak")
        call.output_blocks_after = 4

    recording = await run_idle_call(call, script=transport_stalls)

    [unplayed] = exits.named(UNPLAYED)
    assert unplayed["reason"] == "playback_stalled"
    assert set(unplayed) == {"room_name", "reason", "workflow_run_id", "elapsed_ms"}
    assert recording.ended
    assert exits.refers == []


async def test_tts_already_unavailable_ends_without_queueing_the_goodbye(
    exits, plain_workflow
):
    call = await build_idle_call(plain_workflow, scripted_llm(GREETING, IDLE_PROMPT))

    async def tts_goes_down(call):
        await seen(call, "idle")
        call.failure_exits.state.mark_tts_unavailable("test")

    recording = await run_idle_call(call, script=tts_goes_down)

    assert recording.kinds("speak") == []
    [unplayed] = exits.named(UNPLAYED)
    assert unplayed["reason"] == "tts_unavailable"
    assert unplayed["room_name"] == ROOM and unplayed["workflow_run_id"] == RUN
    assert unplayed["elapsed_ms"] < 200  # nothing was waited for
    idle, decided = recording.times("idle")[1], recording.first("end_call")[1]
    assert decided - idle < 0.2
    assert recording.kinds("end", "cancel") == ["end"]
    assert exits.refers == []


async def test_tts_error_during_the_goodbye_does_not_transfer(
    exits, plain_workflow, monkeypatch
):
    """The real watchdog sees the TTS ErrorFrame mid-goodbye: it marks TTS
    unavailable — which ends the wait — and does not transfer."""
    call = await build_idle_call(
        plain_workflow,
        scripted_llm(GREETING, IDLE_PROMPT),
        tts=_farewell_tts(mode="error", duration_ms=2000),
    )
    _scale(call, monkeypatch)

    recording = await run_idle_call(call)

    [unplayed] = exits.named(UNPLAYED)
    assert unplayed["reason"] == "tts_unavailable"
    assert call.failure_exits.state.tts_unavailable
    assert recording.first("end_call")[2] == IDLE
    assert exits.refers == []
    assert exits.named("safetynet.triggered") == []


def _fake_exits(call):
    return types.SimpleNamespace(state=call.state, watchdog=call.wd)


def _capture_events(monkeypatch):
    events = []
    monkeypatch.setattr(sn, "log_event", lambda event, **f: events.append((event, f)))
    return events


async def test_a_call_already_ending_is_left_alone(services, refer, monkeypatch):
    """The caller hung up while the goodbye was awaited: no event, TTS not
    blamed, no second end."""
    call = await ready_call(make_call(services, refer, tts="silent"))
    events = _capture_events(monkeypatch)
    original = call.engine.queue_frame

    async def queue_frame(frame, direction=FrameDirection.DOWNSTREAM):
        await original(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            call.engine.disposed = True

    call.engine.task.queue_frame = queue_frame
    resumed = []

    await farewell.end_idle_call(
        call.engine, _fake_exits(call), resume=lambda: resumed.append(1)
    )

    assert call.engine.entries("tts") == [("tts", IDLE_FAREWELL_TEXT)]
    assert call.engine.entries("end") == []
    assert events == []
    assert not call.state.tts_unavailable
    assert not call.state.idle_end_in_progress  # the watchdog bounds that end
    assert resumed == []
    await call.wd.stop()


# --- tasks 2.6: the caller speaks up ------------------------------------------


async def test_caller_speaking_before_the_goodbye_starts_calls_it_off(
    exits, plain_workflow
):
    """VAD hears the caller while the goodbye is still on its way: the call
    goes on, the queued goodbye is dropped unheard, and the idle count starts
    over — the next silence gets the idle prompt first."""
    llm = scripted_llm(GREETING, IDLE_PROMPT, "我在，請說。", IDLE_PROMPT)
    call = await build_idle_call(
        plain_workflow, llm, tts=_farewell_tts(first_audio_delay=0.6)
    )
    state = call.failure_exits.state

    async def caller_speaks_up(call):
        await seen(call, "speak")
        assert state.idle_end_in_progress
        await call.caller.makes_a_sound()
        while state.idle_end_in_progress:
            await asyncio.sleep(0.01)
        await call.caller.says("喂")

    recording = await run_idle_call(call, script=caller_speaks_up)

    assert recording.kinds("idle", "llm_asked", "speak", "end_call") == [
        "idle",
        "llm_asked",
        "idle",
        "speak",  # called off
        "idle",
        "llm_asked",  # the idle prompt again, not the goodbye
        "idle",
        "speak",
        "end_call",
    ]
    # greeting, idle prompt, reply, idle prompt, goodbye: the first goodbye
    # never reached the caller.
    assert len(recording.segments_before_end()) == 5
    assert call.tts.received_texts.count(IDLE_FAREWELL_TEXT) == 2
    assert exits.events == []
    assert exits.refers == []


async def test_caller_speaking_while_the_bot_finishes_calls_off_the_goodbye(
    services, refer, monkeypatch
):
    """review gate R23: the caller speaks while the goodbye still waits for
    the idle prompt to finish; that speech calls the hangup off (the mark is
    taken when the idle end starts, not after the wait)."""
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        VADUserStartedSpeakingFrame,
    )

    call = await ready_call(make_call(services, refer, tts="silent"))
    events = _capture_events(monkeypatch)
    await call.wd.on_push_frame(push_data(BotStartedSpeakingFrame()))  # idle prompt
    resumed = []
    hangup = asyncio.create_task(
        farewell.end_idle_call(
            call.engine, _fake_exits(call), resume=lambda: resumed.append(1)
        )
    )
    await asyncio.sleep(0.02)
    await call.wd.on_push_frame(
        push_data(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=0.0))
    )
    await call.wd.on_push_frame(push_data(BotStoppedSpeakingFrame()))
    await hangup

    assert resumed == [1]
    assert call.engine.entries("end") == []
    assert events == []
    assert not call.state.tts_unavailable
    assert not call.state.idle_end_in_progress
    await call.wd.stop()


async def test_the_safetynet_still_transfers_after_a_called_off_hangup(
    exits, plain_workflow
):
    """Standing down did not spend the watchdog: back in the conversation, a
    bot that owes a reply and stays silent is transferred."""
    llm = scripted_llm(GREETING, IDLE_PROMPT, stall_at_step=2)
    call = await build_idle_call(
        plain_workflow, llm, tts=_farewell_tts(first_audio_delay=0.6)
    )
    state = call.failure_exits.state

    async def caller_speaks_up(call):
        await seen(call, "speak")
        await call.caller.makes_a_sound()
        while state.idle_end_in_progress:
            await asyncio.sleep(0.01)
        call.failure_exits.watchdog._threshold = 0.3
        await call.caller.says("喂")
        while not exits.refers:
            await asyncio.sleep(0.01)
        await call.task.cancel()

    recording = await run_idle_call(call, script=caller_speaks_up)

    assert exits.refers == [(ROOM, FALLBACK)]
    [triggered] = exits.named("safetynet.triggered")
    assert triggered["reason"] == "bot_silence"
    assert recording.kinds("end_call").count("end_call") <= 1
    assert all(e[2] != IDLE for e in recording.events if e[0] == "end_call")


async def test_caller_speaking_over_the_goodbye_does_not_stop_the_hangup(
    exits, plain_workflow
):
    call = await build_idle_call(
        plain_workflow,
        scripted_llm(GREETING, IDLE_PROMPT),
        tts=_farewell_tts(duration_ms=1000),
    )

    async def caller_talks_over(call):
        await seen(call, "bot_started", 3)
        await call.caller.makes_a_sound()

    recording = await run_idle_call(call, script=caller_talks_over)

    assert recording.first("end_call")[2] == IDLE
    assert recording.kinds("end", "cancel") == ["end"]
    assert exits.events == []
    assert exits.refers == []


# --- tasks 2.7 (AC3): the silence budget, from the code's constants ----------


def test_every_idle_hangup_branch_fits_the_silence_budget():
    """design D5. The exact sums are pinned as well: a changed constant that
    still fits the budget must be a decision, not a drift."""
    branches = idle_hangup_silence()

    assert branches == pytest.approx(
        {
            "played": 13.35,
            "played_then_end": 5.0,
            "start_timeout": 18.35,
            "playback_stalled": 7.0,
            "tts_unavailable": 13.35,
        }
    )
    assert max(branches.values()) <= SILENCE_BUDGET
    # The wait for the bot to fall silent is the playback gap timeout.
    assert PLAYBACK_GAP_TIMEOUT_SECONDS == 2.0


# --- tasks 2.9: the flow itself fails -----------------------------------------


async def test_an_error_in_the_flow_still_ends_the_call(services, refer, monkeypatch):
    call = await ready_call(make_call(services, refer))
    events = _capture_events(monkeypatch)

    async def queue_frame(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, TTSSpeakFrame):
            raise RuntimeError("queue closed")

    call.engine.task.queue_frame = queue_frame

    await farewell.end_idle_call(call.engine, _fake_exits(call), resume=lambda: None)

    [(event, fields)] = events
    assert event == UNPLAYED
    assert fields["reason"] == "flow_error"
    assert fields["room_name"] == call.wd.room_name
    assert call.engine.entries("end") == [("end", IDLE, False)]
    assert call.engine.entries("refer") == []
    await call.wd.stop()


# --- tasks 2.10: a cold transfer in flight owns the exit ----------------------


async def test_a_transfer_in_flight_stops_the_idle_hangup_from_starting(
    services, refer
):
    call = await ready_call(make_call(services, refer))
    call.engine._livekit_transfer_in_progress = True

    await farewell.end_idle_call(call.engine, _fake_exits(call), resume=lambda: None)

    assert call.engine.log == []
    assert not call.state.idle_end_in_progress
    await call.wd.stop()


async def test_a_transfer_started_during_the_goodbye_keeps_the_call(
    services, refer, monkeypatch
):
    """The caller pressed 0 while the goodbye played."""
    call = await ready_call(make_call(services, refer))
    events = _capture_events(monkeypatch)
    original = call.engine.queue_frame

    async def queue_frame(frame, direction=FrameDirection.DOWNSTREAM):
        await original(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            call.engine._livekit_transfer_in_progress = True

    call.engine.task.queue_frame = queue_frame

    await farewell.end_idle_call(call.engine, _fake_exits(call), resume=lambda: None)

    assert call.engine.entries("tts") == [("tts", IDLE_FAREWELL_TEXT)]
    assert call.engine.entries("end") == []
    assert events == []
    assert not call.state.idle_end_in_progress
    await call.wd.stop()


# --- tasks 2.11: the idle prompt's LLM stream stalled mid-sentence ------------


@pytest.mark.parametrize("interrupt", [True, False], ids=["interrupted", "control"])
async def test_a_stalled_idle_prompt_does_not_hold_the_goodbye_back(
    exits, plain_workflow, monkeypatch, interrupt
):
    """The LLM said the idle prompt and its stream never closed. The goodbye,
    queued at the pipeline source, would wait behind that turn; the flow cuts
    the turn first. Control: without the cut, the goodbye does not play."""
    llm = scripted_llm(GREETING, IDLE_PROMPT + "喂？", stall_at_step=1)
    call = await build_idle_call(plain_workflow, llm)
    _scale(call, monkeypatch)
    if not interrupt:

        async def no_interruption(engine):
            pass

        monkeypatch.setattr(farewell, "_cut_current_turn", no_interruption)

    recording = await run_idle_call(call)

    assert len(recording.times("idle")) == 2
    if interrupt:
        assert len(recording.segments_before_end()) == 3
        assert exits.named(UNPLAYED) == []
    else:
        assert len(recording.segments_before_end()) == 2
        assert [e["reason"] for e in exits.named(UNPLAYED)] == ["start_timeout"]
    assert exits.refers == []


# --- review gate 5.1 (reviewer R4, R6, R9, R14) -------------------------------


async def test_a_call_already_ending_at_the_second_idle_gets_no_goodbye(
    services, refer
):
    """An end already under way (its extraction still running) owns the exit:
    a goodbye on top of it would be a second one."""
    call = await ready_call(make_call(services, refer))
    call.engine.disposed = True

    await farewell.end_idle_call(call.engine, _fake_exits(call), resume=lambda: None)

    assert call.engine.log == []
    assert not call.state.idle_end_in_progress
    await call.wd.stop()


async def test_a_cancelled_idle_hangup_gives_the_call_back_to_the_watchdog(
    services, refer
):
    call = await ready_call(make_call(services, refer, tts="silent"))
    flow = asyncio.create_task(
        farewell.end_idle_call(call.engine, _fake_exits(call), resume=lambda: None)
    )
    await until(lambda: call.engine.entries("tts"))
    assert call.state.idle_end_in_progress

    flow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flow

    assert not call.state.idle_end_in_progress
    await call.wd.stop()


async def test_a_dropped_trigger_is_logged_once_per_reason(services, refer):
    from loguru import logger

    call = await ready_call(make_call(services, refer))
    call.state.idle_end_in_progress = True
    lines = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        for _ in range(3):
            call.wd._fire("bot_silence")
        call.wd._fire("llm_error")
    finally:
        logger.remove(sink)

    dropped = [line for line in lines if "dropped during idle hangup" in line]
    assert len(dropped) == 2
    assert call.engine.entries("refer") == []
    await call.wd.stop()


@pytest.mark.parametrize("kind", ["llm_error", "stt_errors"])
async def test_an_error_dropped_during_a_called_off_hangup_still_transfers(
    services, refer, kind
):
    """review gate R25: a one-shot error trigger dropped while the goodbye was
    under way fires once the caller calls the hangup off — the error does not
    come again, and the call must not go on with a dead provider. (A TTS
    error aborts the goodbye and ends the call itself.)"""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    call = await ready_call(make_call(services, refer, tts="silent"))
    resumed = []
    hangup = asyncio.create_task(
        farewell.end_idle_call(
            call.engine, _fake_exits(call), resume=lambda: resumed.append(1)
        )
    )
    await until(lambda: call.engine.entries("tts"))
    await _trigger(call, services, kind)
    assert call.engine.entries("refer") == []  # dropped while the goodbye runs
    await call.wd.on_push_frame(
        push_data(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=0.0))
    )
    await hangup

    assert resumed == [1]
    await until(lambda: call.engine.entries("refer"))
    assert len(call.engine.entries("refer")) == 1
    await call.wd.stop()


async def test_an_error_dropped_in_an_earlier_idle_end_is_not_replayed_later(
    services, refer
):
    """review round 2 R3(b): an error dropped during one goodbye whose call
    a press-0 transfer then took over (nothing replays it there) must not
    fire when a later, unrelated goodbye is called off."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    call = await ready_call(make_call(services, refer, tts="silent"))
    # First idle end: an LLM error is dropped, the caller presses 0 meanwhile.
    hangup = asyncio.create_task(
        farewell.end_idle_call(call.engine, _fake_exits(call), resume=lambda: None)
    )
    await until(lambda: call.engine.entries("tts"))
    await _trigger(call, services, "llm_error")
    call.engine._livekit_transfer_in_progress = True
    await hangup
    assert call.engine.entries("refer") == []
    # The press-0 REFER failed: back to the AI, which has recovered.
    call.engine._livekit_transfer_in_progress = False
    call.engine.log.clear()

    # A later idle end, called off by the caller; no error this time.
    hangup = asyncio.create_task(
        farewell.end_idle_call(call.engine, _fake_exits(call), resume=lambda: None)
    )
    await until(lambda: call.engine.entries("tts"))
    await call.wd.on_push_frame(
        push_data(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=0.0))
    )
    await hangup
    await settle(0.2)

    assert call.engine.entries("refer") == []
    assert not call.wd._fired
    await call.wd.stop()


async def test_a_called_off_hangup_with_nothing_dropped_does_not_transfer(
    services, refer
):
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    call = await ready_call(make_call(services, refer, tts="silent"))
    hangup = asyncio.create_task(
        farewell.end_idle_call(call.engine, _fake_exits(call), resume=lambda: None)
    )
    await until(lambda: call.engine.entries("tts"))
    call.wd._fire("bot_silence")  # polled: comes back by itself, not replayed
    await call.wd.on_push_frame(
        push_data(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=0.0))
    )
    await hangup
    await settle(0.2)

    assert call.engine.entries("refer") == []
    assert not call.wd._fired
    await call.wd.stop()


async def test_the_forced_end_still_cancels_when_its_bookkeeping_fails(monkeypatch):
    monkeypatch.setattr(farewell, "IDLE_END_DEADLINE_SECONDS", 0)
    cancel, queue_frame = AsyncMock(), AsyncMock()

    def record_call_disposition(reason):
        raise RuntimeError("context gone")

    engine = types.SimpleNamespace(
        task=types.SimpleNamespace(
            has_finished=lambda: False,
            cancel=cancel,
            _pipeline=types.SimpleNamespace(queue_frame=queue_frame),
        ),
        record_call_disposition=record_call_disposition,
    )
    end_task = asyncio.create_task(asyncio.sleep(60))

    with patch(
        "api.services.pipecat.livekit_call_events.delete_room_at_run_end",
        AsyncMock(),
    ):
        await farewell._force_idle_end_after_deadline(engine, end_task, "room-x", 7)

    await asyncio.wait({end_task})
    assert end_task.cancelled()
    cancel.assert_awaited_once()
    queue_frame.assert_awaited_once()
