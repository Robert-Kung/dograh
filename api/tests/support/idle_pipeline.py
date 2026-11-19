"""A pipeline-level harness for the LIVEKIT idle hangup (idle-hangup-farewell).

Builds the LLM/TTS part of the production pipeline with the real
``LLMContextAggregatorPair`` (its idle timer and the downstream-then-upstream
route an ``LLMMessagesAppendFrame`` takes are what the idle hangup depends
on), the real engine, the real safetynet watchdog and LLM gates, a mock LLM
and a mock TTS. The caller never speaks unless a test makes them
(:class:`Caller`), so the idle timer fires twice.

What reaches the caller is recorded at the output transport: every audio
write, the bot-speaking frames the transport emits, and the ``EndFrame`` /
``CancelFrame`` passing it. :class:`Recording` turns that into the speech
segments the caller heard before the call ended. A frame trace through the
processors is kept as well, to say where a prompt died.
"""

import asyncio
import time
import types
from typing import Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    LLMContextFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    UserSpeakingFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_mute import (
    CallbackUserMuteStrategy,
    MuteUntilFirstBotCompleteUserMuteStrategy,
)
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy
from pipecat.turns.user_stop import ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.time import time_now_iso8601

from api.enums import WorkflowRunMode
from api.services.pipecat.empty_llm_turn_fallback import EmptyLLMTurnFallbackProcessor
from api.services.pipecat.livekit_failure_exits import SafetynetLLMGate
from api.services.pipecat.livekit_safetynet import LiveKitFailureExits
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.workflow_graph import WorkflowGraph
from pipecat.tests import MockLLMService, MockTTSService

ROOM = "cs-_+886277001234_idle"
RUN = 4242

# Scaled-down caller silence before each idle event.
IDLE_TIMEOUT = 0.4
# Length of every mock TTS utterance.
UTTERANCE_MS = 300

GREETING = "您好，這裡是客服中心。"
IDLE_PROMPT = "請問您還在線上嗎？"
LLM_FAREWELL = "祝您有美好的一天，再見。"

TRACED = (
    LLMMessagesAppendFrame,
    LLMContextFrame,
    LLMFullResponseStartFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    EndFrame,
    CancelFrame,
)


class DelayedMockLLM(MockLLMService):
    """A mock LLM whose replies take ``response_delay`` to start.

    ``inference_delay`` is the same for ``run_inference`` (the variable
    extraction); ``None`` there means it never returns. ``stall_at_step``:
    the reply of that turn (0-based) is streamed and then never ends.
    """

    def __init__(
        self,
        *,
        response_delay: float = 0.0,
        inference_delay: Optional[float] = 0.0,
        stall_at_step: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.response_delay = response_delay
        self.inference_delay = inference_delay
        self.stall_at_step = stall_at_step
        self.inference_calls = 0

    async def _stream_mock_chunks(self):
        if self.response_delay > 0:
            await asyncio.sleep(self.response_delay)
        stall = self.get_current_step() == self.stall_at_step
        async for chunk in super()._stream_mock_chunks():
            yield chunk
        if stall:
            await asyncio.Event().wait()

    async def run_inference(self, context, max_tokens=None, system_instruction=None):
        self.inference_calls += 1
        if self.inference_delay is None:
            await asyncio.Event().wait()
        elif self.inference_delay > 0:
            await asyncio.sleep(self.inference_delay)
        return await super().run_inference(
            context, max_tokens=max_tokens, system_instruction=system_instruction
        )


class ScriptedTTS(MockTTSService):
    """A mock TTS that treats utterances containing ``marker`` differently.

    ``mode``: ``play`` (speak it), ``hang`` (never produce audio), ``error``
    (some audio, then a non-fatal ErrorFrame and nothing more).
    ``first_audio_delay`` and ``duration_ms`` apply to ``play`` and ``error``.
    """

    def __init__(
        self,
        *,
        marker: str,
        mode: str = "play",
        first_audio_delay: float = 0.0,
        duration_ms: int = UTTERANCE_MS,
        **kwargs,
    ):
        kwargs.setdefault("mock_audio_duration_ms", UTTERANCE_MS)
        kwargs.setdefault("frame_delay", 0.001)
        super().__init__(**kwargs)
        self.marker = marker
        self.mode = mode
        self.first_audio_delay = first_audio_delay
        self.duration_ms = duration_ms

    async def run_tts(self, text: str, context_id: str):
        if self.marker not in text:
            async for frame in super().run_tts(text, context_id):
                yield frame
            return
        self.received_texts.append(text)
        if self.first_audio_delay > 0:
            await asyncio.sleep(self.first_audio_delay)
        if self.mode == "hang":
            await asyncio.Event().wait()
        audio = self.create_mock_audio(self.duration_ms)
        for sent, i in enumerate(range(0, len(audio), self._chunk_size)):
            yield TTSAudioRawFrame(
                audio=audio[i : i + self._chunk_size],
                sample_rate=16000,
                num_channels=1,
                context_id=context_id,
            )
            await asyncio.sleep(self._frame_delay)
            if self.mode == "error" and sent == 2:
                await self.push_error("mock TTS failure")
                await asyncio.Event().wait()


class Caller(FrameProcessor):
    """Directly after the input transport: the caller, when a test needs one."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    async def makes_a_sound(self) -> None:
        """What VAD reports when the caller starts to speak."""
        await self.push_frame(VADUserStartedSpeakingFrame())

    async def says(self, text: str) -> None:
        """A whole user turn."""
        await self.push_frame(VADUserStartedSpeakingFrame())
        await self.push_frame(UserStartedSpeakingFrame())
        await asyncio.sleep(0)
        await self.broadcast_frame(UserSpeakingFrame)
        await asyncio.sleep(0)
        await self.push_frame(TranscriptionFrame(text, "user", time_now_iso8601()))
        await asyncio.sleep(0)
        await self.push_frame(UserStoppedSpeakingFrame())


class Recording:
    """What went out to the caller, in order, with monotonic times."""

    def __init__(self):
        self.events: list[tuple] = []
        self.trace: list[str] = []

    def add(self, kind: str, *detail) -> None:
        self.events.append((kind, time.monotonic(), *detail))

    def _until_end(self) -> list[tuple]:
        out = []
        for event in self.events:
            if event[0] in ("end", "cancel"):
                break
            out.append(event)
        return out

    @property
    def ended(self) -> bool:
        return any(e[0] in ("end", "cancel") for e in self.events)

    @property
    def end_time(self) -> Optional[float]:
        return next((e[1] for e in self.events if e[0] in ("end", "cancel")), None)

    def segments_before_end(self) -> list[int]:
        """Bytes of audio in each bot utterance written before the call ended."""
        segments: list[int] = []
        for event in self._until_end():
            if event[0] == "bot_started":
                segments.append(0)
            elif event[0] == "audio" and segments:
                segments[-1] += event[2]
        return segments

    def kinds(self, *kinds: str) -> list[str]:
        """The events of these kinds, in order."""
        return [e[0] for e in self.events if e[0] in kinds]

    def first(self, kind: str) -> Optional[tuple]:
        return next((e for e in self.events if e[0] == kind), None)

    def audio_after_end(self) -> int:
        seen_end = False
        total = 0
        for event in self.events:
            if event[0] in ("end", "cancel"):
                seen_end = True
            elif seen_end and event[0] == "audio":
                total += event[2]
        return total

    def times(self, kind: str) -> list[float]:
        return [e[1] for e in self.events if e[0] == kind]


class _OutputTap(FrameProcessor):
    """Directly after the output transport: what it let through, and when."""

    def __init__(self, recording: Recording, **kwargs):
        super().__init__(**kwargs)
        self._recording = recording

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction is FrameDirection.DOWNSTREAM:
            if isinstance(frame, BotStartedSpeakingFrame):
                self._recording.add("bot_started")
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._recording.add("bot_stopped")
            elif isinstance(frame, EndFrame):
                self._recording.add("end")
            elif isinstance(frame, CancelFrame):
                self._recording.add("cancel")
        await self.push_frame(frame, direction)


class _FrameTrace(BaseObserver):
    """Hop-by-hop route of the frames the idle hangup turns on."""

    def __init__(self, recording: Recording, **kwargs):
        super().__init__(**kwargs)
        self._recording = recording
        self._spoken: set[int] = set()

    async def on_push_frame(self, data) -> None:
        frame = data.frame
        if isinstance(frame, TTSSpeakFrame) and frame.id not in self._spoken:
            self._spoken.add(frame.id)
            self._recording.add("speak", frame.text)
        if isinstance(frame, TRACED):
            arrow = "->" if data.direction is FrameDirection.DOWNSTREAM else "<-"
            self._recording.trace.append(
                f"{type(frame).__name__} {data.source.name} {arrow} "
                f"{data.destination.name}"
            )


class IdleCall(types.SimpleNamespace):
    """The pieces of one harness call: engine, task, llm, tts, recording, ..."""


def livekit_run(*, room_name: Optional[str] = ROOM):
    context = {"room_name": room_name} if room_name else {}
    return types.SimpleNamespace(
        id=RUN, mode=WorkflowRunMode.LIVEKIT.value, initial_context=context
    )


def other_mode_run():
    return types.SimpleNamespace(
        id=RUN, mode=WorkflowRunMode.SMALLWEBRTC.value, initial_context={}
    )


def scripted_llm(*texts: str, **kwargs) -> DelayedMockLLM:
    """A mock LLM that says ``texts`` one per turn, then nothing."""
    steps = [MockLLMService.create_text_chunks(text) for text in texts]
    return DelayedMockLLM(mock_steps=steps, chunk_delay=0.001, **kwargs)


async def build_idle_call(
    workflow: WorkflowGraph,
    llm: DelayedMockLLM,
    *,
    workflow_run=None,
    tts: Optional[MockTTSService] = None,
    idle_timeout: float = IDLE_TIMEOUT,
    is_realtime: bool = False,
) -> IdleCall:
    """Assemble the call the way ``_run_pipeline_impl`` does, around mocks."""
    workflow_run = workflow_run if workflow_run is not None else livekit_run()
    livekit = workflow_run.mode == WorkflowRunMode.LIVEKIT.value
    recording = Recording()
    # output_blocks_after: audio writes the transport still takes before it
    # stops taking any (None: never stops).
    call = IdleCall(output_blocks_after=None)

    tts = tts or MockTTSService(mock_audio_duration_ms=UTTERANCE_MS, frame_delay=0.001)
    transport = MockTransport(
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
        ),
    )
    output = transport.output()
    write_audio_frame = output.write_audio_frame

    async def recording_write(frame):
        if call.output_blocks_after is not None:
            if call.output_blocks_after <= 0:
                await asyncio.Event().wait()
            call.output_blocks_after -= 1
        written = await write_audio_frame(frame)
        if written and any(frame.audio):
            # The transport pads the end of the call with silence.
            recording.add("audio", len(frame.audio))
        return written

    output.write_audio_frame = recording_write

    context = LLMContext()
    engine = PipecatEngine(
        llm=llm,
        context=context,
        workflow=workflow,
        call_context_vars={},
        workflow_run_id=workflow_run.id,
        workflow_run_mode=workflow_run.mode,
        room_name=(workflow_run.initial_context or {}).get("room_name"),
    )

    user_params = LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            start=[TranscriptionUserTurnStartStrategy()],
            stop=[ExternalUserTurnStopStrategy()],
        ),
        user_mute_strategies=[
            MuteUntilFirstBotCompleteUserMuteStrategy(),
            CallbackUserMuteStrategy(should_mute_callback=engine.should_mute_user),
        ],
        user_idle_timeout=idle_timeout,
    )
    aggregators = LLMContextAggregatorPair(
        context,
        assistant_params=LLMAssistantAggregatorParams(),
        user_params=user_params,
    )
    user_aggregator = aggregators.user()

    failure_exits = LiveKitFailureExits(workflow_run, None, is_realtime=is_realtime)

    idle_handler = engine.create_user_idle_handler(failure_exits)

    push_frame = user_aggregator.push_frame

    async def recording_push(frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, LLMMessagesAppendFrame) and frame.run_llm:
            recording.add("llm_asked")
        await push_frame(frame, direction)

    user_aggregator.push_frame = recording_push

    end_call_with_reason = engine.end_call_with_reason

    async def recording_end(reason, abort_immediately=False):
        recording.add("end_call", reason)
        await end_call_with_reason(reason, abort_immediately=abort_immediately)

    engine.end_call_with_reason = recording_end

    @user_aggregator.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        recording.add("idle")
        await idle_handler.handle_idle(aggregator)

    @user_aggregator.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy):
        idle_handler.reset()

    llm_stage = [llm]
    if failure_exits.state is not None:
        llm_stage = [
            SafetynetLLMGate(failure_exits.state),
            llm,
            SafetynetLLMGate(failure_exits.state),
        ]
    caller = Caller()
    pipeline = Pipeline(
        [
            transport.input(),
            caller,
            user_aggregator,
            *llm_stage,
            EmptyLLMTurnFallbackProcessor(),
            tts,
            output,
            _OutputTap(recording),
            aggregators.assistant(),
        ]
    )
    task = PipelineWorker(pipeline, params=PipelineParams(), enable_rtvi=False)
    engine.set_task(task)
    task.add_observer(_FrameTrace(recording))
    if livekit:
        await failure_exits.install_watchdog(engine, task)

    call.__dict__.update(
        engine=engine,
        caller=caller,
        task=task,
        llm=llm,
        tts=tts,
        transport=transport,
        recording=recording,
        failure_exits=failure_exits,
        idle_handler=idle_handler,
        user_aggregator=user_aggregator,
    )
    return call


async def run_idle_call(call: IdleCall, *, script=None, timeout: float = 20.0):
    """Greet, then leave the caller silent until the call ends.

    ``script``: an ``async def (call)`` run alongside the call, for what the
    caller or the services do meanwhile.
    """
    from api.services.pipecat.worker_runner import run_pipeline_worker

    async def start():
        await asyncio.sleep(0.01)
        await call.engine.initialize()
        await call.engine.set_node(call.engine.workflow.start_node_id)
        await call.engine.llm.queue_frame(LLMContextFrame(call.engine.context))
        if script is not None:
            await script(call)

    try:
        await asyncio.wait_for(
            asyncio.gather(run_pipeline_worker(call.task), start()), timeout=timeout
        )
    finally:
        watchdog = call.failure_exits.watchdog
        if watchdog is not None:
            await watchdog.stop()
    return call.recording


async def seen(call: IdleCall, kind: str, count: int = 1, timeout: float = 10.0):
    """Wait until the recording holds ``count`` events of ``kind``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(call.recording.times(kind)) < count:
        if loop.time() > deadline:
            raise AssertionError(f"{count} x {kind!r} not reached")
        await asyncio.sleep(0.01)
