"""Per-call state for the voice-service failure exits (la-only-go-live D9).

Every non-fatal ErrorFrame on a LIVEKIT call gets a defined exit instead of the
immediate cancel ``on_pipeline_error`` used to apply to all of them:

- **STT**: ``STT_ERROR_THRESHOLD`` failures inside ``STT_ERROR_WINDOW_SECONDS``
  → transfer. The window covers the slowest failure shape, a Gemini session
  setup that hangs until its 5 s timeout and retries a second later; errors
  closer together than ``STT_ERROR_MERGE_SECONDS`` are one failure (Cloud STT
  reports a mid-stream failure twice). A transcription, or an error after a
  connection that stayed up ``STT_STABLE_CONNECTION_SECONDS``, starts the
  count over — so a single blip (a Gemini Live session ending without
  ``go_away``) never transfers, but a key that fails right after each
  successful setup still does.
- **TTS**: the call is marked TTS-unavailable and transferred without a
  prompt; a failed REFER then ends the call without the end prompt.
- **LLM** and **unclassified** sources: transferred at once, no retry — the
  shared key means a 429 is a project-wide quota state, not a blip.

The transfer itself is :func:`livekit_safetynet.midcall_safetynet`; this
module holds what it needs to read and drive: whether TTS is still usable,
playback progress as the output transport reports it, and the switches that
stop the LLM and STT.
"""

import asyncio
import time
from collections import deque
from collections.abc import Callable
from enum import Enum
from typing import Optional

from loguru import logger

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    CancelFrame,
    ErrorFrame,
    Frame,
    LLMContextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

STT_ERROR_THRESHOLD = 3
# Gemini STT: 5 s setup timeout + 1 s retry interval → a hung setup fails
# every ~6 s; three of those span 12 s.
STT_ERROR_WINDOW_SECONDS = 15.0
STT_ERROR_MERGE_SECONDS = 0.5
STT_STABLE_CONNECTION_SECONDS = 5.0

# Playback is judged at the output transport (BotStarted/BotSpeaking/
# BotStoppedSpeaking), never at the TTS output: a one-shot TTS hands over the
# whole utterance at once and then goes quiet while the transport is still
# playing it. The transport emits BotSpeakingFrame every 0.2 s while audio
# goes out, so a 2 s gap is ten missed heartbeats.
PLAYBACK_START_TIMEOUT_SECONDS = 3.0
PLAYBACK_GAP_TIMEOUT_SECONDS = 2.0


class ErrorSource(str, Enum):
    STT = "stt"
    TTS = "tts"
    LLM = "llm"
    UNCLASSIFIED = "unclassified"


def classify_error_source(frame: ErrorFrame) -> ErrorSource:
    """Which voice service pushed ``frame`` (``push_error`` stamps ``processor``)."""
    processor = frame.processor
    if isinstance(processor, STTService):
        return ErrorSource.STT
    if isinstance(processor, TTSService):
        return ErrorSource.TTS
    if isinstance(processor, LLMService):
        return ErrorSource.LLM
    return ErrorSource.UNCLASSIFIED


class SttErrorWindow:
    """Counts STT failures in a sliding window; True once the threshold is met."""

    def __init__(
        self,
        *,
        threshold: int = STT_ERROR_THRESHOLD,
        window_seconds: float = STT_ERROR_WINDOW_SECONDS,
        merge_seconds: float = STT_ERROR_MERGE_SECONDS,
        stable_seconds: float = STT_STABLE_CONNECTION_SECONDS,
    ):
        self._threshold = threshold
        self._window = window_seconds
        self._merge = merge_seconds
        self._stable = stable_seconds
        self._times: deque[float] = deque()
        self._connected_at: Optional[float] = None

    def record(self, now: float) -> bool:
        if self._connected_at is not None:
            if now - self._connected_at >= self._stable:
                self._times.clear()
            self._connected_at = None
        if self._times and now - self._times[-1] < self._merge:
            return False
        self._times.append(now)
        while self._times and now - self._times[0] > self._window:
            self._times.popleft()
        return len(self._times) >= self._threshold

    def connected(self, now: float) -> None:
        self._connected_at = now

    def reset(self) -> None:
        self._times.clear()
        self._connected_at = None


class PlaybackOutcome(str, Enum):
    PLAYED = "played"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"  # the caller spoke before it started
    ABORTED = "aborted"  # TTS reported an error meanwhile


class PlaybackTracker:
    """Output-transport playback progress, for bounded failure-path prompts.

    Frames are observed once per processor hop and delivered through the
    observer queue, so a frame counts once per id (the transport's upstream
    and downstream copies are two ids — both counters move in step) and
    progress is read as counts, not clock comparisons.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._seen: deque = deque(maxlen=64)
        self.speaking = False
        self.started = 0
        self.stopped = 0
        # stopped count at each start, so a short prompt whose start and end
        # both land before the waiter looks is still seen to have finished
        self._stopped_at_start: list[int] = []
        self.user_spoke = 0
        self._last_audio = clock()
        self._changed = asyncio.Event()

    def _first_sight(self, frame: Frame) -> bool:
        if frame.id in self._seen:
            return False
        self._seen.append(frame.id)
        return True

    def _audio(self) -> None:
        self.speaking = True
        self._last_audio = self._clock()
        self._changed.set()

    def note_started(self, frame: Frame) -> None:
        if self._first_sight(frame):
            self._stopped_at_start.append(self.stopped)
            self.started += 1
            self._audio()

    def note_audio(self, frame: Frame) -> None:
        if self._first_sight(frame):
            self._audio()

    def note_stopped(self, frame: Frame) -> None:
        if self._first_sight(frame):
            self.stopped += 1
            self.speaking = False
            self._changed.set()

    def note_user_speech(self, frame: Frame) -> None:
        if self._first_sight(frame):
            self.user_spoke += 1
            self._changed.set()

    def poke(self) -> None:
        """Wake waiters so they re-check their abort condition."""
        self._changed.set()

    async def _wait_change(self, remaining: float) -> None:
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass

    async def wait_idle(self, timeout: float) -> None:
        """Until the bot is not speaking (an interruption just went out)."""
        deadline = self._clock() + timeout
        while self.speaking:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            await self._wait_change(remaining)

    async def wait_played(
        self,
        *,
        started_mark: int,
        user_mark: int,
        start_timeout: float,
        gap_timeout: float,
        abort: Callable[[], bool],
    ) -> PlaybackOutcome:
        """How a prompt queued when the counters stood at the marks played out.

        Played once a bot speech that started after the mark has stopped.
        Timed out when nothing starts within ``start_timeout``, or audio stops
        arriving for ``gap_timeout`` without the transport reporting the end;
        healthy long prompts keep producing heartbeats and are never cut by
        their length.
        """
        queued_at = self._clock()
        stopped_at_start: Optional[int] = None
        while True:
            if abort():
                return PlaybackOutcome.ABORTED
            if stopped_at_start is None:
                if self.started > started_mark:
                    stopped_at_start = self._stopped_at_start[started_mark]
                    continue
                if self.user_spoke > user_mark:
                    return PlaybackOutcome.INTERRUPTED
                limit = queued_at + start_timeout
            else:
                if self.stopped > stopped_at_start:
                    return PlaybackOutcome.PLAYED
                limit = self._last_audio + gap_timeout
            remaining = limit - self._clock()
            if remaining <= 0:
                return PlaybackOutcome.TIMED_OUT
            await self._wait_change(remaining)


class CallFailureState:
    """What the failure exits know about one call."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        playback_start_timeout: float = PLAYBACK_START_TIMEOUT_SECONDS,
        playback_gap_timeout: float = PLAYBACK_GAP_TIMEOUT_SECONDS,
    ):
        self.stt_errors = SttErrorWindow()
        self.playback = PlaybackTracker(clock)
        self.playback_start_timeout = playback_start_timeout
        self.playback_gap_timeout = playback_gap_timeout
        self.tts_unavailable = False
        self.llm_gate_closed = False
        self._stt: Optional[STTService] = None

    def attach_stt(self, stt: Optional[STTService]) -> None:
        self._stt = stt

    def mark_tts_unavailable(self, why: str) -> None:
        if not self.tts_unavailable:
            logger.warning(f"TTS marked unavailable for this call: {why}")
        self.tts_unavailable = True
        self.playback.poke()

    async def play_bounded(self, engine, text: str) -> bool:
        """Speak a fixed prompt, bounded by the playback timeout (D9).

        Skipped when TTS is already unavailable. Waits for the bot to fall
        silent first, so the tail of an interrupted utterance is not taken
        for this prompt. A timeout marks TTS unavailable; the caller talking
        over the prompt before it starts does not. Either way the caller
        moves on. Fixed text through ``TTSSpeakFrame`` — never the LLM.
        """
        if self.tts_unavailable:
            return False
        tracker = self.playback
        await tracker.wait_idle(self.playback_gap_timeout)
        started_mark, user_mark = tracker.started, tracker.user_spoke
        try:
            await engine.task.queue_frame(TTSSpeakFrame(text, persist_to_logs=True))
        except Exception as e:
            self.mark_tts_unavailable(f"prompt not queued: {e}")
            return False
        outcome = await tracker.wait_played(
            started_mark=started_mark,
            user_mark=user_mark,
            start_timeout=self.playback_start_timeout,
            gap_timeout=self.playback_gap_timeout,
            abort=lambda: self.tts_unavailable,
        )
        if outcome is PlaybackOutcome.TIMED_OUT:
            self.mark_tts_unavailable("prompt playback timed out")
        return outcome is PlaybackOutcome.PLAYED

    async def stop_stt(self) -> None:
        """From the REFER on, STT must not reconnect (one failed request each).

        Cancelling the STT service tears its connection down and, for both
        Google STT flavours, ends the reconnect loop; the pipeline's own
        CancelFrame/EndFrame later finds it already disconnected.
        """
        stt, self._stt = self._stt, None
        if stt is None:
            return
        try:
            await stt.cancel(CancelFrame(reason="safetynet_refer"))
        except Exception as e:
            logger.warning(f"STT stop before REFER failed: {e}")


class SafetynetLLMGate(FrameProcessor):
    """Keeps the LLM from running while the call is being transferred.

    Placed directly before and directly after the LLM. Closed, it drops LLM
    context frames in both directions, and the upstream BotStoppedSpeakingFrame
    an LLM uses to release function calls it deferred until the bot finished
    speaking.
    """

    def __init__(self, state: CallFailureState, **kwargs):
        super().__init__(**kwargs)
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if self._state.llm_gate_closed and (
            isinstance(frame, LLMContextFrame)
            or (
                isinstance(frame, BotStoppedSpeakingFrame)
                and direction is FrameDirection.UPSTREAM
            )
        ):
            return
        await self.push_frame(frame, direction)
