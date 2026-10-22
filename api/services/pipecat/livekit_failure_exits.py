"""Per-call state for the voice-service failure exits (la-only-go-live D9).

Every non-fatal ErrorFrame on a LIVEKIT call gets a defined exit instead of the
immediate cancel ``on_pipeline_error`` used to apply to all of them:

- **STT**: ``STT_ERROR_THRESHOLD`` errors inside ``STT_ERROR_WINDOW_SECONDS`` →
  transfer. Both Google STT flavours reconnect every second and push one
  non-fatal ErrorFrame per failure, so a dead key reaches the threshold in a
  few seconds, while a single blip (a Gemini Live session ending without
  ``go_away``) does not. A transcription or a successful reconnect resets the
  count.
- **TTS**: the call is marked TTS-unavailable and transferred without a
  prompt; a failed REFER then ends the call without the end prompt.
- **LLM** and **unclassified** sources: transferred at once, no retry — the
  shared key means a 429 is a project-wide quota state, not a blip.

The transfer itself is :func:`livekit_safetynet.midcall_safetynet`; this
module holds what it needs to read and drive: whether TTS is still usable,
playback progress as the output transport reports it, and the switch that
stops STT and the LLM once the REFER goes out.
"""

import asyncio
import time
from collections import deque
from collections.abc import Callable
from enum import Enum
from typing import Optional

from loguru import logger

from pipecat.frames.frames import CancelFrame, ErrorFrame, Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

STT_ERROR_THRESHOLD = 3
STT_ERROR_WINDOW_SECONDS = 10.0

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
    """Counts STT errors in a sliding window; True once the threshold is met."""

    def __init__(
        self,
        *,
        threshold: int = STT_ERROR_THRESHOLD,
        window_seconds: float = STT_ERROR_WINDOW_SECONDS,
    ):
        self._threshold = threshold
        self._window = window_seconds
        self._times: deque[float] = deque()

    def record(self, now: float) -> bool:
        self._times.append(now)
        while self._times and now - self._times[0] > self._window:
            self._times.popleft()
        return len(self._times) >= self._threshold

    def reset(self) -> None:
        self._times.clear()


class PlaybackTracker:
    """Output-transport playback progress, for bounded failure-path prompts."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last_audio: Optional[float] = None
        self._last_stopped: Optional[float] = None
        self._changed = asyncio.Event()

    def note_audio(self) -> None:
        self._last_audio = self._clock()
        self._changed.set()

    def note_stopped(self) -> None:
        self._last_stopped = self._clock()
        self._changed.set()

    def poke(self) -> None:
        """Wake waiters so they re-check their abort condition."""
        self._changed.set()

    async def wait_played(
        self,
        since: float,
        *,
        start_timeout: float,
        gap_timeout: float,
        abort: Callable[[], bool],
    ) -> bool:
        """True once audio that started after ``since`` has stopped.

        False when no audio starts within ``start_timeout``, when audio stops
        arriving for ``gap_timeout`` without the transport reporting the end,
        or when ``abort()`` turns true. Healthy long prompts keep producing
        heartbeats and are never cut by their length.
        """
        started: Optional[float] = None
        while True:
            if abort():
                return False
            if started is None and self._last_audio is not None:
                if self._last_audio >= since:
                    started = self._last_audio
            if (
                started is not None
                and self._last_stopped is not None
                and self._last_stopped >= started
            ):
                return True
            if started is None:
                limit = since + start_timeout
            else:
                limit = self._last_audio + gap_timeout
            remaining = limit - self._clock()
            if remaining <= 0:
                return False
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass


class CallFailureState:
    """What the failure exits know about one call."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        playback_start_timeout: float = PLAYBACK_START_TIMEOUT_SECONDS,
        playback_gap_timeout: float = PLAYBACK_GAP_TIMEOUT_SECONDS,
    ):
        self.clock = clock
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

        Skipped when TTS is already unavailable. A timeout or a TTS error
        during the prompt marks TTS unavailable; either way the caller moves
        on. Fixed text through ``TTSSpeakFrame`` — never through the LLM.
        """
        from pipecat.frames.frames import TTSSpeakFrame

        if self.tts_unavailable:
            return False
        since = self.clock()
        try:
            await engine.task.queue_frame(TTSSpeakFrame(text, persist_to_logs=True))
        except Exception as e:
            self.mark_tts_unavailable(f"prompt not queued: {e}")
            return False
        played = await self.playback.wait_played(
            since,
            start_timeout=self.playback_start_timeout,
            gap_timeout=self.playback_gap_timeout,
            abort=lambda: self.tts_unavailable,
        )
        if not played:
            self.mark_tts_unavailable("prompt playback timed out")
        return played

    async def stop_stt_and_llm(self) -> None:
        """From the REFER on, nothing may reach STT (reconnects) or the LLM.

        Cancelling the STT service tears its connection down and, for both
        Google STT flavours, ends the reconnect loop; the pipeline's own
        CancelFrame/EndFrame later finds it already disconnected. The LLM is
        stopped by the gates in front of and behind it.
        """
        self.llm_gate_closed = True
        stt, self._stt = self._stt, None
        if stt is None:
            return
        try:
            await stt.cancel(CancelFrame(reason="safetynet_refer"))
        except Exception as e:
            logger.warning(f"STT stop before REFER failed: {e}")


class SafetynetLLMGate(FrameProcessor):
    """Drops LLM context frames once the call is being transferred.

    Placed directly before and directly after the LLM, so a context pushed
    downstream by the user aggregator and one pushed upstream from behind the
    LLM are both stopped.
    """

    def __init__(self, state: CallFailureState, **kwargs):
        super().__init__(**kwargs)
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if self._state.llm_gate_closed and isinstance(frame, LLMContextFrame):
            return
        await self.push_frame(frame, direction)
