"""Voice-service failure exits (la-only-go-live 1.4, design D9).

Every non-fatal ErrorFrame on a LIVEKIT call has a defined exit, the transfer
prompt and the end prompt are bounded by the playback timeout measured at the
output transport, STT and the LLM stop as the REFER goes out, and a failed
REFER ends the call — without the end prompt when TTS is unavailable.

Errors are injected with fakes that live only in this file (D9: no runtime
fault injection). The ErrorFrames come from the real Gemini STT/TTS and
Google LLM service objects (never started), so classification runs against
the processor types the pipeline actually has.
"""

import asyncio
import itertools
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import errors as genai_errors
from pipecat.frames.frames import (
    BotSpeakingFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    LLMContextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.stt import GeminiSTTService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.google.tts import GeminiTTSService
from pipecat.utils.enums import EndTaskReason

from api.services.pipecat import livekit_safetynet as sn
from api.services.pipecat.livekit_failure_exits import (
    STT_ERROR_THRESHOLD,
    STT_ERROR_WINDOW_SECONDS,
    STT_STABLE_CONNECTION_SECONDS,
    CallFailureState,
    ErrorSource,
    PlaybackOutcome,
    SafetynetLLMGate,
    classify_error_source,
)

FAKE_KEY = "AIza" + "0" * 35
ROOM = "cs-+886912345678"
RUN = 42
FALLBACK = "tel:+886900000000"
TRANSFERRED = EndTaskReason.TRANSFER_CALL.value

# Short playback timeouts so the timeout cases run in well under a second.
START_TIMEOUT = 0.2
GAP_TIMEOUT = 0.15


@pytest.fixture(autouse=True)
def _reset_latch():
    sn._fired_runs.clear()
    sn._fired_order.clear()
    yield


@pytest.fixture(scope="module")
def services():
    return types.SimpleNamespace(
        stt=GeminiSTTService(api_key=FAKE_KEY),
        tts=GeminiTTSService(api_key=FAKE_KEY, use_genai=True),
        llm=GoogleLLMService(api_key=FAKE_KEY),
    )


def _stt_error(services, code=1011):
    exc = genai_errors.APIError(code, {"error": {"message": "stream closed"}}, None)
    return ErrorFrame(
        error=f"Gemini STT connection error: {exc}",
        exception=exc,
        processor=services.stt,
    )


def _tts_error(services, code=429):
    exc = genai_errors.ClientError(
        code, {"error": {"code": code, "status": "RESOURCE_EXHAUSTED"}}, None
    )
    return ErrorFrame(
        error=f"Gemini GenAI TTS generation error: {exc}",
        exception=exc,
        processor=services.tts,
    )


def _llm_429(services):
    exc = genai_errors.ClientError(
        429,
        {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}},
        None,
    )
    return ErrorFrame(
        error=f"Unknown error occurred: {exc}", exception=exc, processor=services.llm
    )


def _push(frame):
    return types.SimpleNamespace(frame=frame)


# --- classification --------------------------------------------------------


def test_errors_are_classified_by_the_service_that_pushed_them(services):
    assert classify_error_source(_stt_error(services)) is ErrorSource.STT
    assert classify_error_source(_tts_error(services)) is ErrorSource.TTS
    assert classify_error_source(_llm_429(services)) is ErrorSource.LLM
    assert (
        classify_error_source(ErrorFrame(error="?", processor=None))
        is ErrorSource.UNCLASSIFIED
    )


# --- fakes -----------------------------------------------------------------


class FakeSTT:
    """Records being stopped and keeps 'reconnecting' until it is."""

    def __init__(self, order):
        self.cancelled = False
        self._order = order

    async def cancel(self, frame):
        self.cancelled = True
        self._order.append("stt_stopped")


class FakeEngine:
    """Engine with a scripted TTS, recording the call's exit in order.

    ``tts``: ``healthy`` (a prompt plays and ends), ``silent`` (no audio ever),
    ``stall`` (audio starts, then stops arriving), ``error`` (the TTS pushes
    an ErrorFrame for the prompt, seen by the watchdog).
    """

    def __init__(self, state, *, tts="healthy", watchdog=None, services=None):
        self.state = state
        self.tts = tts
        self.watchdog = watchdog
        self.services = services
        self.log = []
        self._livekit_transfer_in_progress = False
        self.task = types.SimpleNamespace(
            queue_frame=self.queue_frame, has_finished=lambda: True
        )

    async def queue_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, TTSSpeakFrame):
            self.log.append(("tts", frame.text))
            if self.tts == "healthy":
                self.state.playback.note_started(BotStartedSpeakingFrame())
                self.state.playback.note_stopped(BotStoppedSpeakingFrame())
            elif self.tts == "stall":
                self.state.playback.note_started(BotStartedSpeakingFrame())
            elif self.tts == "error":
                await self.watchdog.on_push_frame(_push(_tts_error(self.services)))

    async def end_call_with_reason(self, reason, abort_immediately=False):
        self.log.append(("end", reason, abort_immediately))

    disposed = False

    def is_call_disposed(self):
        return self.disposed or any(entry[0] == "end" for entry in self.log)

    async def resolve_transfer_call_config(self):
        return {"destination": FALLBACK}

    def entries(self, kind):
        return [e for e in self.log if e[0] == kind]


@pytest.fixture
def refer(monkeypatch):
    """Replace the SIP REFER with a scripted one; record its order and args."""
    from api.services.pipecat import livekit_call_events, livekit_cold_transfer

    script = types.SimpleNamespace(
        result={"status": "success"}, calls=[], hang=False, order=[]
    )

    async def fake_cold_transfer(room_name, destination, **kwargs):
        script.calls.append({"room": room_name, "to": destination, **kwargs})
        script.engine.log.append(("refer", kwargs.get("play_dialtone")))
        script.order.append("refer")
        if script.hang:
            await asyncio.sleep(3600)
        return script.result

    async def fake_delete_room(room_name, workflow_run_id, lk=None):
        script.engine.log.append(("bye", room_name))

    monkeypatch.setattr(
        livekit_cold_transfer, "cold_transfer_to_human", fake_cold_transfer
    )
    monkeypatch.setattr(livekit_call_events, "delete_room_at_run_end", fake_delete_room)
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)
    monkeypatch.setattr(sn, "log_event", lambda *a, **k: None)
    return script


def _call(services, refer, *, tts="healthy"):
    """A watchdog built the way run_pipeline builds it, with a fake engine."""
    from api.enums import WorkflowRunMode

    state = CallFailureState(
        playback_start_timeout=START_TIMEOUT, playback_gap_timeout=GAP_TIMEOUT
    )
    stt = FakeSTT(refer.order)
    state.attach_stt(stt)
    engine = FakeEngine(state, tts=tts, services=services)
    run = types.SimpleNamespace(
        id=RUN,
        mode=WorkflowRunMode.LIVEKIT.value,
        initial_context={"room_name": ROOM},
    )
    return _Call(state=state, stt=stt, engine=engine, run=run, refer=refer)


class _Call(types.SimpleNamespace):
    """Holds the pieces; ``await call.ready()`` builds the real watchdog."""


async def _ready(call):
    call.wd = await sn.resolve_safetynet_watchdog(call.engine, call.run, call.state)
    ticks = itertools.count()
    # Each reading is a second later: back-to-back errors arrive at the STT
    # reconnect cadence. Tests that need exact times set their own clock.
    call.wd._clock = lambda: float(next(ticks))
    call.wd._poll_seconds = 0.02
    call.wd._threshold = 999.0  # owed-reply silence is not under test here
    call.engine.watchdog = call.wd
    call.refer.engine = call.engine
    return call


async def _settle(seconds=0.05):
    """Let the watchdog's background exit run."""
    for _ in range(int(seconds / 0.01) + 1):
        await asyncio.sleep(0.01)


async def _until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


# --- on_pipeline_error -----------------------------------------------------


def _register_handlers(monkeypatch, engine, watchdog):
    from api.services.pipecat import event_handlers

    handlers = {}

    def event_handler(name):
        def deco(fn):
            handlers[name] = fn
            return fn

        return deco

    task = types.SimpleNamespace(event_handler=event_handler, turn_trace_observer=None)
    transport = types.SimpleNamespace(add_event_handler=lambda *a: None)
    audio_buffer = MagicMock()
    audio_buffer.start_recording = AsyncMock()
    audio_buffer.stop_recording = AsyncMock()
    monkeypatch.setattr(
        event_handlers.db_client,
        "get_workflow_run_by_id",
        AsyncMock(return_value=None),
    )
    event_handlers.register_event_handlers(
        task,
        transport,
        workflow_run_id=RUN,
        engine=engine,
        audio_buffer=audio_buffer,
        in_memory_logs_buffer=MagicMock(),
        pipeline_metrics_aggregator=MagicMock(),
        audio_config=None,
        safetynet_watchdog=watchdog,
    )
    return handlers["on_pipeline_error"]


@pytest.mark.asyncio
async def test_single_nonfatal_stt_error_does_not_end_the_call(
    monkeypatch, services, refer
):
    call = await _ready(_call(services, refer))
    on_pipeline_error = _register_handlers(monkeypatch, call.engine, call.wd)
    frame = _stt_error(services)  # e.g. a Gemini Live session ending
    await call.wd.on_push_frame(_push(frame))
    await on_pipeline_error(None, frame)
    await _settle()
    assert call.engine.log == []  # no cancel, no REFER, no BYE
    await call.wd.stop()


@pytest.mark.asyncio
async def test_fatal_error_still_cancels(monkeypatch, services, refer):
    call = await _ready(_call(services, refer))
    on_pipeline_error = _register_handlers(monkeypatch, call.engine, call.wd)
    await on_pipeline_error(None, ErrorFrame(error="boom", fatal=True))
    assert ("end", "pipeline_error", True) in call.engine.log
    await call.wd.stop()


@pytest.mark.asyncio
async def test_without_the_watchdog_errors_keep_cancelling(monkeypatch, services):
    """Non-LIVEKIT runs (and a LIVEKIT run whose watchdog did not install)
    keep the upstream behaviour: they have no transfer exit to hand over to."""
    engine = FakeEngine(CallFailureState())
    on_pipeline_error = _register_handlers(monkeypatch, engine, None)
    await on_pipeline_error(None, _stt_error(services))
    assert engine.log == [("end", "pipeline_error", True)]


# --- STT: consecutive errors → transfer -------------------------------------


@pytest.mark.asyncio
async def test_stt_errors_reaching_the_threshold_transfer(services, refer):
    t = [0.0]
    call = await _ready(_call(services, refer))
    call.wd._clock = lambda: t[0]
    for i in range(STT_ERROR_THRESHOLD):
        t[0] = float(i)  # the 1 s reconnect cadence
        await call.wd.on_push_frame(_push(_stt_error(services)))
    await _until(lambda: call.engine.entries("refer"))
    assert call.engine.entries("refer") == [("refer", True)]
    await call.wd.stop()


@pytest.mark.asyncio
async def test_stt_errors_spread_beyond_the_window_do_not_transfer(services, refer):
    t = [0.0]
    call = await _ready(_call(services, refer))
    call.wd._clock = lambda: t[0]
    spacing = STT_ERROR_WINDOW_SECONDS / (STT_ERROR_THRESHOLD - 1) + 0.5
    for i in range(STT_ERROR_THRESHOLD * 2):
        t[0] = i * spacing
        await call.wd.on_push_frame(_push(_stt_error(services)))
    await _settle()
    assert call.engine.log == []
    await call.wd.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["transcription", "stable_reconnect"])
async def test_stt_recovery_resets_the_count(services, refer, recovery):
    t = [0.0]
    call = await _ready(_call(services, refer))
    call.wd._clock = lambda: t[0]
    for i in range(STT_ERROR_THRESHOLD - 1):
        t[0] = float(i)
        await call.wd.on_push_frame(_push(_stt_error(services)))
    if recovery == "transcription":
        await call.wd.on_push_frame(
            _push(TranscriptionFrame(text="你好", user_id="u", timestamp="t"))
        )
    else:
        call.wd.note_stt_connected()
        t[0] += STT_STABLE_CONNECTION_SECONDS  # stayed up, then dropped
    await call.wd.on_push_frame(_push(_stt_error(services)))
    await _settle()
    assert call.engine.log == []
    await call.wd.stop()


@pytest.mark.asyncio
async def test_one_stt_error_is_counted_once_across_processor_hops(services, refer):
    call = await _ready(_call(services, refer))
    frame = _stt_error(services)
    for _ in range(STT_ERROR_THRESHOLD + 2):  # same frame seen at every hop
        await call.wd.on_push_frame(_push(frame))
    await _settle()
    assert call.engine.log == []
    await call.wd.stop()


# --- LLM / TTS / unclassified → transfer ------------------------------------


@pytest.mark.asyncio
async def test_llm_429_transfers_with_prompt_when_tts_works(services, refer):
    call = await _ready(_call(services, refer))
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("refer"))
    await _until(lambda: call.engine.entries("end"))
    # transfer prompt, REFER, then the transferred call's normal end
    assert [e[0] for e in call.engine.log] == ["tts", "refer", "end"]
    assert "轉接專員" in call.engine.log[0][1]
    assert call.engine.log[2] == ("end", TRANSFERRED, False)
    assert call.stt.cancelled and call.state.llm_gate_closed
    await call.wd.stop()


@pytest.mark.asyncio
async def test_llm_429_transfers_without_prompt_when_tts_unavailable(services, refer):
    call = await _ready(_call(services, refer))
    call.state.mark_tts_unavailable("earlier TTS error")
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("end"))
    assert call.engine.log == [("refer", True), ("end", TRANSFERRED, False)]
    await call.wd.stop()


@pytest.mark.asyncio
async def test_llm_429_refer_rejected_ends_with_bye(services, refer):
    refer.result = {"status": "failed", "reason": "sip_refer_rejected"}
    call = await _ready(_call(services, refer))
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("end"))
    kinds = [e[0] for e in call.engine.log]
    # transfer prompt, REFER, end prompt (TTS works), graceful end
    assert kinds == ["tts", "refer", "tts", "end"]
    assert "請稍後再撥" in call.engine.log[2][1]
    assert call.engine.log[3] == ("end", "pipeline_error", False)
    await call.wd.stop()


@pytest.mark.asyncio
async def test_llm_429_refer_rejected_tts_unavailable_bye_without_prompt(
    services, refer
):
    refer.result = {"status": "failed", "reason": "sip_refer_rejected"}
    call = await _ready(_call(services, refer))
    call.state.mark_tts_unavailable("earlier TTS error")
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("bye"))
    assert call.engine.log == [
        ("refer", True),
        ("end", "pipeline_error", True),
        ("bye", ROOM),
    ]
    await call.wd.stop()


@pytest.mark.asyncio
async def test_tts_error_transfers_without_prompt(services, refer):
    call = await _ready(_call(services, refer))
    await call.wd.on_push_frame(_push(_tts_error(services)))
    await _until(lambda: call.engine.entries("end"))
    assert call.state.tts_unavailable
    assert call.engine.log == [("refer", True), ("end", TRANSFERRED, False)]
    await call.wd.stop()


@pytest.mark.asyncio
async def test_unclassified_error_is_not_silent(services, refer):
    call = await _ready(_call(services, refer))
    await call.wd.on_push_frame(_push(ErrorFrame(error="from somewhere else")))
    await _until(lambda: call.engine.entries("refer"))
    assert call.engine.entries("refer")
    await call.wd.stop()


@pytest.mark.asyncio
async def test_single_stt_error_is_not_taken_for_llm_or_unclassified(services, refer):
    call = await _ready(_call(services, refer))
    await call.wd.on_push_frame(_push(_stt_error(services, code=1008)))
    await _settle()
    assert call.engine.log == []
    assert not call.wd._fired
    await call.wd.stop()


# --- bounded prompt playback -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tts", ["silent", "stall", "error"])
async def test_stuck_or_failing_tts_does_not_hold_the_refer(services, refer, tts):
    call = await _ready(_call(services, refer, tts=tts))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("refer"))
    assert loop.time() - t0 < START_TIMEOUT + GAP_TIMEOUT + 0.3
    assert call.state.tts_unavailable
    await call.wd.stop()


@pytest.mark.asyncio
async def test_stuck_tts_and_rejected_refer_bye_within_the_timeout(services, refer):
    refer.result = {"status": "failed", "reason": "sip_refer_rejected"}
    call = await _ready(_call(services, refer, tts="silent"))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("bye"))
    assert loop.time() - t0 < START_TIMEOUT + 0.3
    # Only the transfer prompt was tried; no end prompt after the timeout.
    assert [e[0] for e in call.engine.log] == ["tts", "refer", "end", "bye"]
    await call.wd.stop()


async def _play_long_prompt(state, wd, *, one_shot):
    """1 s of healthy playback, as the output transport reports it.

    ``one_shot``: the TTS hands over all audio at once (GoogleHttpTTSService
    shape) and then goes quiet; the transport keeps playing it. Streaming:
    audio arrives as it plays. Either way only the transport's bot-speaking
    frames say whether the caller is hearing something.
    """
    audio = TTSAudioRawFrame(audio=b"\x01\x00" * 160, sample_rate=16000, num_channels=1)
    if one_shot:
        for _ in range(20):
            await wd.on_push_frame(_push(audio))
    await wd.on_push_frame(_push(BotStartedSpeakingFrame()))
    for _ in range(10):
        await asyncio.sleep(0.1)  # well under GAP_TIMEOUT, far over it in total
        if not one_shot:
            await wd.on_push_frame(_push(audio))
        await wd.on_push_frame(_push(BotSpeakingFrame()))
    await wd.on_push_frame(_push(BotStoppedSpeakingFrame()))


@pytest.mark.asyncio
@pytest.mark.parametrize("one_shot", [True, False], ids=["one_shot", "streaming"])
async def test_healthy_long_prompt_is_not_cut(services, refer, one_shot):
    call = await _ready(_call(services, refer, tts="silent"))  # playback driven below
    tracker = call.state.playback
    player = asyncio.create_task(
        _play_long_prompt(call.state, call.wd, one_shot=one_shot)
    )
    outcome = await tracker.wait_played(
        started_mark=tracker.started,
        user_mark=tracker.user_spoke,
        start_timeout=START_TIMEOUT,
        gap_timeout=GAP_TIMEOUT,
        abort=lambda: call.state.tts_unavailable,
    )
    await player
    assert outcome is PlaybackOutcome.PLAYED
    assert not call.state.tts_unavailable
    await call.wd.stop()


# --- one REFER per call; after the REFER --------------------------------------


@pytest.mark.asyncio
async def test_two_paths_on_one_call_send_one_refer(services, refer):
    """STT threshold and owed-reply silence firing together: one REFER."""
    call = await _ready(_call(services, refer))
    for _ in range(STT_ERROR_THRESHOLD):
        await call.wd.on_push_frame(_push(_stt_error(services)))
    await asyncio.gather(
        sn.midcall_safetynet(
            call.engine,
            room_name=ROOM,
            reason="bot_silence",
            workflow_run_id=RUN,
            state=call.state,
        ),
        _until(lambda: call.engine.entries("refer")),
    )
    await _settle()
    assert len(call.engine.entries("refer")) == 1
    await call.wd.stop()


@pytest.mark.asyncio
async def test_after_refer_stt_is_stopped_and_its_errors_do_nothing(services, refer):
    refer.hang = True  # the REFER is ringing
    call = await _ready(_call(services, refer))
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("refer"))
    assert call.stt.cancelled  # no more reconnects, no more failed requests
    for _ in range(STT_ERROR_THRESHOLD * 2):
        await call.wd.on_push_frame(_push(_stt_error(services)))
    await _settle()
    assert len(call.engine.entries("refer")) == 1
    assert call.engine.entries("end") == [] and call.engine.entries("bye") == []
    await call.wd.stop()


@pytest.mark.asyncio
async def test_stt_is_stopped_before_the_refer(services, refer):
    call = await _ready(_call(services, refer))
    await call.wd.on_push_frame(_push(_tts_error(services)))
    await _until(lambda: call.engine.entries("refer"))
    assert refer.order == ["stt_stopped", "refer"]
    await call.wd.stop()


@pytest.mark.asyncio
async def test_end_prompt_after_failed_refer_does_not_reach_the_llm(services, refer):
    """The end prompt is fixed text; the LLM gate is already closed."""
    refer.result = {"status": "failed", "reason": "sip_refer_rejected"}
    call = await _ready(_call(services, refer))
    seen_gate = []
    original_queue = call.engine.queue_frame

    async def queue_frame(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, TTSSpeakFrame):
            seen_gate.append(call.state.llm_gate_closed)
        await original_queue(frame, direction)

    call.engine.task = types.SimpleNamespace(queue_frame=queue_frame)
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("end"))
    assert seen_gate == [True, True]  # gated from the trigger on
    await call.wd.stop()


@pytest.mark.asyncio
async def test_key_failure_combination(services, refer):
    """Shared key dead: STT keeps failing, TTS fails, the REFER is rejected →
    one REFER without prompt, then BYE without the end prompt."""
    refer.result = {"status": "failed", "reason": "sip_refer_rejected"}
    call = await _ready(_call(services, refer, tts="error"))
    await call.wd.on_push_frame(_push(_tts_error(services)))
    for _ in range(STT_ERROR_THRESHOLD):
        await call.wd.on_push_frame(_push(_stt_error(services, code=1008)))
    await _until(lambda: call.engine.entries("bye"))
    await _settle()
    assert call.engine.log == [
        ("refer", True),
        ("end", "pipeline_error", True),
        ("bye", ROOM),
    ]
    await call.wd.stop()


# --- LLM gate --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "direction", [FrameDirection.DOWNSTREAM, FrameDirection.UPSTREAM]
)
async def test_llm_gate_drops_context_frames_once_closed(direction):
    state = CallFailureState()
    gate = SafetynetLLMGate(state)
    pushed = []

    async def push_frame(frame, d=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    gate.push_frame = push_frame
    context = LLMContextFrame(context=LLMContext())
    await gate.process_frame(context, direction)
    assert pushed == [context]
    state.llm_gate_closed = True
    await gate.process_frame(LLMContextFrame(context=LLMContext()), direction)
    speak = TTSSpeakFrame("固定話術")
    await gate.process_frame(speak, direction)
    assert pushed == [context, speak]


@pytest.mark.asyncio
async def test_stopping_gemini_stt_latches_its_reconnects(services):
    state = CallFailureState()
    stt = GeminiSTTService(api_key=FAKE_KEY)
    state.attach_stt(stt)
    await state.stop_stt()
    assert stt._stopped  # _connect / reconnect loop no longer open sessions


# --- REFER bounds ----------------------------------------------------------


def test_transfer_settle_bound_outlasts_the_livekit_refer_timeout():
    """Our transfer watchdog must not cut a transfer that is still ringing."""
    from livekit.api._dial_timeout import dial_timeout
    from livekit.protocol.sip import TransferSIPParticipantRequest

    from api.services.pipecat.livekit_transfer_flow import (
        TRANSFER_SETTLE_TIMEOUT_SECONDS,
    )

    assert TRANSFER_SETTLE_TIMEOUT_SECONDS > dial_timeout(
        None, TransferSIPParticipantRequest()
    )


@pytest.mark.asyncio
async def test_refer_reported_failed_without_raising_is_a_failure():
    from livekit.protocol.sip import SIPTransferStatus

    from api.services.pipecat.livekit_cold_transfer import (
        SIP_KIND,
        cold_transfer_to_human,
    )

    async def list_participants(req):
        return types.SimpleNamespace(
            participants=[types.SimpleNamespace(kind=SIP_KIND, identity="caller")]
        )

    async def transfer(req):
        return types.SimpleNamespace(
            status=SIPTransferStatus.STS_TRANSFER_FAILED,
            reason="busy",
            sip_status=486,
        )

    lk = types.SimpleNamespace(
        room=types.SimpleNamespace(list_participants=list_participants),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
    )
    result = await cold_transfer_to_human(ROOM, FALLBACK, lk=lk)
    assert result["status"] == "failed"
    assert result["reason"] == "sip_refer_rejected"


@pytest.mark.asyncio
async def test_refer_timeout_is_a_failure_and_ends_the_call(services, refer):
    """The LiveKit client's own timeout raises; the flow reports it failed."""
    refer.result = {
        "status": "failed",
        "action": "transfer_failed",
        "reason": "sip_refer_error",
        "message": "TimeoutError",
    }
    call = await _ready(_call(services, refer))
    call.state.mark_tts_unavailable("earlier TTS error")
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("bye"))
    assert [e[0] for e in call.engine.log] == ["refer", "end", "bye"]
    await call.wd.stop()


# --- review gate regressions (la-only 1.4 review R1–R9) -----------------------


@pytest.mark.asyncio
async def test_failed_inflight_transfer_rearms_the_watchdog(
    monkeypatch, services, refer
):
    """R1: the safetynet yields to a press-0 transfer; that REFER fails and the
    call is back with the AI. A still-dead LLM must get its exit again."""
    from api.services.pipecat import livekit_transfer_flow

    monkeypatch.setattr(livekit_transfer_flow, "TRANSFER_SETTLE_TIMEOUT_SECONDS", 1.0)
    call = await _ready(_call(services, refer))
    call.wd._poll_seconds = 0.02
    call.engine._livekit_transfer_in_progress = True  # press-0 REFER ringing
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _settle(0.1)
    assert call.engine.log == []  # yielded
    call.engine._livekit_transfer_in_progress = False  # it failed: back to AI
    await _settle(0.1)
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("refer"))
    assert len(call.engine.entries("refer")) == 1
    await call.wd.stop()


@pytest.mark.asyncio
async def test_rearm_waits_for_the_inflight_transfer_and_skips_ended_calls(
    services, refer
):
    call = await _ready(_call(services, refer))
    call.wd._poll_seconds = 0.02
    call.engine._livekit_transfer_in_progress = True
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _settle(0.1)
    assert call.wd._fired  # still spent while the other transfer runs
    call.engine.disposed = True  # that transfer succeeded and ended the call
    call.engine._livekit_transfer_in_progress = False
    await _settle(0.1)
    assert call.wd._fired
    await call.wd.stop()


@pytest.mark.asyncio
async def test_no_refer_on_a_call_that_is_already_ending(monkeypatch, services, refer):
    """R8: the goodbye's TTS fails after end_call; no transfer on top of it —
    but the ending is bounded (it may be stuck on the same dead LLM)."""
    bounded = []

    async def fake_force_end(engine, room_name, workflow_run_id):
        bounded.append(room_name)

    monkeypatch.setattr(sn, "_force_end_after_deadline", fake_force_end)
    call = await _ready(_call(services, refer))
    call.engine.disposed = True
    await call.wd.on_push_frame(_push(_tts_error(services)))
    await _settle(0.1)
    assert call.engine.log == []
    assert bounded == [ROOM]


@pytest.mark.asyncio
async def test_reconnect_that_fails_at_once_does_not_reset_the_count(services, refer):
    """R2: setup succeeds, the stream is closed straight away, again and again."""
    t = [0.0]
    call = await _ready(_call(services, refer))
    call.wd._clock = lambda: t[0]
    for i in range(STT_ERROR_THRESHOLD):
        t[0] = i * 1.2
        call.wd.note_stt_connected()
        t[0] += 0.2
        await call.wd.on_push_frame(_push(_stt_error(services)))
    await _until(lambda: call.engine.entries("refer"))
    await call.wd.stop()


@pytest.mark.asyncio
async def test_hung_setup_failures_reach_the_threshold(services, refer):
    """R3: 5 s setup timeout + 1 s retry → one error every ~6 s."""
    t = [0.0]
    call = await _ready(_call(services, refer))
    call.wd._clock = lambda: t[0]
    for at in (5.0, 11.0, 17.0):
        t[0] = at
        await call.wd.on_push_frame(_push(_stt_error(services)))
    await _until(lambda: call.engine.entries("refer"))
    await call.wd.stop()


@pytest.mark.asyncio
async def test_one_failure_reported_twice_counts_once(services, refer):
    """R7b: Cloud STT pushes two ErrorFrames for one mid-stream failure."""
    t = [0.0]
    call = await _ready(_call(services, refer))
    call.wd._clock = lambda: t[0]
    for at in (0.0, 0.01, 1.0, 1.01):
        t[0] = at
        await call.wd.on_push_frame(_push(_stt_error(services)))
    await _settle()
    assert call.engine.log == []  # two failures, not four
    await call.wd.stop()


@pytest.mark.asyncio
async def test_tail_of_an_interrupted_utterance_is_not_the_prompt(services, refer):
    """R4: the greeting is still sending heartbeats when the prompt is queued;
    only speech that starts after the bot fell silent counts."""
    call = await _ready(_call(services, refer, tts="silent"))
    await call.wd.on_push_frame(_push(BotStartedSpeakingFrame()))
    play = asyncio.create_task(call.state.play_bounded(call.engine, "提示"))
    await asyncio.sleep(0.02)
    await call.wd.on_push_frame(_push(BotSpeakingFrame()))  # greeting tail
    await call.wd.on_push_frame(_push(BotStoppedSpeakingFrame()))  # interrupted
    assert await play is False
    assert call.state.tts_unavailable  # the prompt itself never played


@pytest.mark.asyncio
async def test_caller_talking_over_the_prompt_does_not_condemn_tts(services, refer):
    """R5: the caller's speech clears the prompt before it starts."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    call = await _ready(_call(services, refer, tts="silent"))
    play = asyncio.create_task(call.state.play_bounded(call.engine, "提示"))
    await asyncio.sleep(0.02)
    await call.wd.on_push_frame(
        _push(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=0.0))
    )
    assert await play is False
    assert not call.state.tts_unavailable


@pytest.mark.asyncio
async def test_closed_gate_holds_back_deferred_function_calls():
    """R9: the LLM releases deferred tool calls on an upstream BotStopped."""
    state = CallFailureState()
    gate = SafetynetLLMGate(state)
    pushed = []

    async def push_frame(frame, d=FrameDirection.DOWNSTREAM):
        pushed.append((frame, d))

    gate.push_frame = push_frame
    state.llm_gate_closed = True
    up = BotStoppedSpeakingFrame()
    down = BotStoppedSpeakingFrame()
    await gate.process_frame(up, FrameDirection.UPSTREAM)
    await gate.process_frame(down, FrameDirection.DOWNSTREAM)
    assert pushed == [(down, FrameDirection.DOWNSTREAM)]


@pytest.mark.asyncio
async def test_settle_bound_starts_after_the_transfer_prompt(monkeypatch):
    """R6: a long prompt before the REFER must not count against its bound."""
    from api.services.pipecat import livekit_transfer_flow

    monkeypatch.setattr(livekit_transfer_flow, "TRANSFER_SETTLE_TIMEOUT_SECONDS", 0.2)
    ended = []

    async def end_call_with_reason(reason, abort_immediately=False):
        ended.append(reason)

    async def queue_frame(frame, *a):
        pass

    engine = types.SimpleNamespace(
        task=types.SimpleNamespace(queue_frame=queue_frame),
        end_call_with_reason=end_call_with_reason,
        is_call_disposed=lambda: bool(ended),
    )

    async def long_prompt():
        await asyncio.sleep(0.3)

    async def refer_after_ringing(room_name, destination, **kwargs):
        await asyncio.sleep(0.1)
        return {"status": "success"}

    from api.services.pipecat import livekit_cold_transfer

    monkeypatch.setattr(
        livekit_cold_transfer, "cold_transfer_to_human", refer_after_ringing
    )
    result = await livekit_transfer_flow.execute_cold_transfer(
        engine,
        room_name=ROOM,
        destination=FALLBACK,
        before_refer=long_prompt,
        before_refer_bounded=True,
        transfer_reason="safetynet",
    )
    assert result["status"] == "success"
    assert livekit_transfer_flow.TRANSFER_UNKNOWN_REASON not in ended


@pytest.mark.asyncio
async def test_stt_threshold_with_tts_down_and_refer_rejected(services, refer):
    """SEC-43: the STT path, too, ends without the end prompt when TTS is down."""
    refer.result = {"status": "failed", "reason": "sip_refer_rejected"}
    call = await _ready(_call(services, refer))
    call.state.mark_tts_unavailable("earlier TTS error")
    for _ in range(STT_ERROR_THRESHOLD):
        await call.wd.on_push_frame(_push(_stt_error(services)))
    await _until(lambda: call.engine.entries("bye"))
    assert call.engine.log == [
        ("refer", True),
        ("end", "pipeline_error", True),
        ("bye", ROOM),
    ]
    await call.wd.stop()


def test_build_pipeline_puts_the_gates_around_the_llm():
    from api.services.pipecat.pipeline_builder import build_pipeline

    marks = {name: types.SimpleNamespace(name=name) for name in ("stt", "llm", "tts")}
    transport = types.SimpleNamespace(
        input=lambda: types.SimpleNamespace(name="in"),
        output=lambda: types.SimpleNamespace(name="out"),
    )
    captured = {}

    import api.services.pipecat.pipeline_builder as pb

    original = pb.Pipeline
    pb.Pipeline = lambda processors: captured.setdefault("p", processors)
    try:
        state = CallFailureState()
        build_pipeline(
            transport,
            marks["stt"],
            object(),
            marks["llm"],
            marks["tts"],
            object(),
            object(),
            object(),
            object(),
            failure_state=state,
        )
    finally:
        pb.Pipeline = original
    processors = captured["p"]
    i = processors.index(marks["llm"])
    assert isinstance(processors[i - 1], SafetynetLLMGate)
    assert isinstance(processors[i + 1], SafetynetLLMGate)
    assert processors[i - 1]._state is state and processors[i + 1]._state is state


# --- recheck regressions (la-only 1.4 recheck R13–R17, R29) -------------------


@pytest.mark.asyncio
async def test_late_yield_touches_nothing_and_releases_the_latch(
    monkeypatch, services, refer
):
    """A voice transfer wins the race inside execute_cold_transfer: the
    safetynet has not gated the LLM, interrupted, or stopped STT."""
    from api.services.pipecat import livekit_transfer_flow

    async def already(engine, **kwargs):
        return {"status": "failed", "reason": "already_transferring"}

    monkeypatch.setattr(livekit_transfer_flow, "execute_cold_transfer", already)
    call = await _ready(_call(services, refer))
    outcome = await sn.midcall_safetynet(
        call.engine,
        room_name=ROOM,
        reason="llm_error",
        workflow_run_id=RUN,
        state=call.state,
    )
    assert outcome == "yielded"
    assert not call.state.llm_gate_closed and not call.stt.cancelled
    assert sn.claim(RUN)  # released
    await call.wd.stop()


@pytest.mark.asyncio
async def test_rearm_restarts_the_silence_clock(services, refer):
    """After a failed press-0 transfer the AI owes the caller a word; if it
    stays silent, bot_silence transfers — not at once, and not never."""
    call = await _ready(_call(services, refer))
    call.wd._clock = time.monotonic
    call.engine._livekit_transfer_in_progress = True
    await call.wd.on_push_frame(_push(_llm_429(services)))  # yields
    await _settle(0.05)
    call.wd._threshold = 0.3
    call.engine._livekit_transfer_in_progress = False  # it failed: back to AI
    await _settle(0.1)
    assert call.engine.entries("refer") == []  # not immediately
    await _until(lambda: call.engine.entries("refer"))
    await call.wd.stop()


@pytest.mark.asyncio
async def test_rearm_gives_up_when_the_call_ends_meanwhile(services, refer):
    call = await _ready(_call(services, refer))
    call.engine._livekit_transfer_in_progress = True  # stuck, never clears
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _settle(0.05)
    call.engine.disposed = True  # its settle bound ended the call
    await _settle(0.1)
    assert call.wd._fired
    await call.wd.stop()


@pytest.mark.asyncio
async def test_voice_transfer_prompt_stays_inside_the_settle_bound(monkeypatch):
    """R14: only the safetynet's self-bounded prompt pauses the bound."""
    from api.services.pipecat import livekit_cold_transfer, livekit_transfer_flow

    monkeypatch.setattr(livekit_transfer_flow, "TRANSFER_SETTLE_TIMEOUT_SECONDS", 0.1)
    ended = []

    async def end_call_with_reason(reason, abort_immediately=False):
        ended.append(reason)

    async def queue_frame(frame, *a):
        pass

    engine = types.SimpleNamespace(
        task=types.SimpleNamespace(queue_frame=queue_frame),
        end_call_with_reason=end_call_with_reason,
        is_call_disposed=lambda: bool(ended),
    )

    async def stuck_recording_fetch():
        await asyncio.sleep(0.3)

    async def refer_ok(room_name, destination, **kwargs):
        return {"status": "success"}

    monkeypatch.setattr(livekit_cold_transfer, "cold_transfer_to_human", refer_ok)
    await livekit_transfer_flow.execute_cold_transfer(
        engine,
        room_name=ROOM,
        destination=FALLBACK,
        before_refer=stuck_recording_fetch,
        transfer_reason="voice_tool",
    )
    assert livekit_transfer_flow.TRANSFER_UNKNOWN_REASON in ended


@pytest.mark.asyncio
async def test_transfer_prompt_talked_over_then_refer_rejected_byes_at_once(
    services, refer
):
    """R17: the transfer prompt never played out (the caller spoke into a
    stuck TTS) → after a rejected REFER, BYE without the end prompt."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    refer.result = {"status": "failed", "reason": "sip_refer_rejected"}
    call = await _ready(_call(services, refer, tts="silent"))
    original = call.engine.queue_frame

    async def queue_frame(frame, direction=FrameDirection.DOWNSTREAM):
        await original(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            await call.wd.on_push_frame(
                _push(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=0.0))
            )

    call.engine.task = types.SimpleNamespace(
        queue_frame=queue_frame, has_finished=lambda: True
    )
    await call.wd.on_push_frame(_push(_llm_429(services)))
    await _until(lambda: call.engine.entries("bye"))
    assert [e[0] for e in call.engine.log] == ["tts", "refer", "end", "bye"]
    assert not call.state.tts_unavailable  # not blamed, but not retried either
    await call.wd.stop()
