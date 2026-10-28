"""Fakes for the LIVEKIT call-exit tests (la-only-go-live 1.4, design D9).

Shared by ``test_livekit_failure_exits`` and ``test_livekit_llm_stall_exits``:
a fake engine with a scripted TTS that records the call's exit in order, a fake
STT, and a builder for the real safetynet watchdog around them. The
``services``, ``refer``, ``reset_safetynet_latch`` and
``no_leaked_background_tasks`` fixtures live in ``api/tests/conftest.py``.

Errors are injected with these fakes only (D9: no runtime fault injection).
"""

import asyncio
import itertools
import types

from google.genai import errors as genai_errors
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.enums import EndTaskReason

from api.services.pipecat import livekit_safetynet as sn
from api.services.pipecat.livekit_failure_exits import CallFailureState

FAKE_KEY = "AIza" + "0" * 35
ROOM = "cs-+886912345678"
RUN = 42
FALLBACK = "tel:+886900000000"
TRANSFERRED = EndTaskReason.TRANSFER_CALL.value

# Short playback timeouts so the timeout cases run in well under a second.
START_TIMEOUT = 0.2
GAP_TIMEOUT = 0.15


def tts_error(services, code=429):
    exc = genai_errors.ClientError(
        code, {"error": {"code": code, "status": "RESOURCE_EXHAUSTED"}}, None
    )
    return ErrorFrame(
        error=f"Gemini GenAI TTS generation error: {exc}",
        exception=exc,
        processor=services.tts,
    )


def push_data(frame):
    return types.SimpleNamespace(frame=frame)


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
                await self.watchdog.on_push_frame(push_data(tts_error(self.services)))

    async def end_call_with_reason(self, reason, abort_immediately=False):
        self.log.append(("end", reason, abort_immediately))

    disposed = False

    def is_call_disposed(self):
        return self.disposed or any(entry[0] == "end" for entry in self.log)

    async def resolve_transfer_call_config(self):
        return {"destination": FALLBACK}

    def entries(self, kind):
        return [e for e in self.log if e[0] == kind]


def make_call(services, refer, *, tts="healthy"):
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
    return Call(state=state, stt=stt, engine=engine, run=run, refer=refer)


class Call(types.SimpleNamespace):
    """Holds the pieces; ``await ready_call(call)`` builds the real watchdog."""


async def ready_call(call):
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


async def settle(seconds=0.05):
    """Let the watchdog's background exit run."""
    for _ in range(int(seconds / 0.01) + 1):
        await asyncio.sleep(0.01)


async def until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)
