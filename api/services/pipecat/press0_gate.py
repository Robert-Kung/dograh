"""Press-0 DTMF cold-transfer gate (S-L3-PRESS0).

A lightweight FrameProcessor placed right after the LiveKit transport input so
it sees ``InputDTMFFrame`` before the LLM. Pressing ``0`` at any point triggers
the shared, business-hours-gated cold transfer (see :mod:`livekit_transfer_flow`)
reusing the workflow's ``transfer_call`` tool config. Any other key passes
through untouched — the gate only recognizes ``0``, never treating caller DTMF
as a transfer destination (C6).
"""

import time
from collections.abc import Callable

from loguru import logger

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import Frame, InputDTMFFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

_DEBOUNCE_SECONDS = 0.5


def debounce_ok(last: float, now: float, window: float) -> bool:
    """True if a trigger at ``now`` is outside the debounce ``window`` after ``last``."""
    return (now - last) >= window


class Press0Gate(FrameProcessor):
    """Cold-transfer the caller when they press ``0``; forward every other frame.

    The triggering ``0`` is swallowed (never forwarded to the LLM). Rapid repeat
    presses inside the debounce window are ignored so an anxious caller mashing
    ``0`` triggers a single transfer.
    """

    def __init__(
        self,
        engine,
        *,
        room_name: str,
        config: dict,
        debounce_seconds: float = _DEBOUNCE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._engine = engine
        self._room_name = room_name
        self._destination = (config.get("destination") or "").strip()
        self._schedule = config.get("schedule")
        self._after_hours_action = config.get("afterHoursAction")
        self._alternate_destination = config.get("alternateDestination")
        self._after_hours_message = config.get("afterHoursMessage")
        self._debounce_seconds = debounce_seconds
        self._monotonic = monotonic  # NB: not _clock — FrameProcessor owns that
        self._last_trigger = float("-inf")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputDTMFFrame) and frame.button == KeypadEntry.ZERO:
            await self._on_zero()
            return  # swallow the trigger digit; never forward 0 downstream

        await self.push_frame(frame, direction)

    async def _on_zero(self):
        now = self._monotonic()
        if not debounce_ok(self._last_trigger, now, self._debounce_seconds):
            logger.debug("press-0 within debounce window; ignoring repeat")
            return
        self._last_trigger = now

        # Barge-in: stop any in-flight TTS before transferring (avoid overlap).
        await self.broadcast_interruption()

        result = await execute_cold_transfer(
            self._engine,
            room_name=self._room_name,
            destination=self._destination,
            schedule=self._schedule,
            after_hours_action=self._after_hours_action,
            alternate_destination=self._alternate_destination,
            after_hours_message=self._after_hours_message,
        )
        logger.info(f"press-0 cold transfer result: {result}")
