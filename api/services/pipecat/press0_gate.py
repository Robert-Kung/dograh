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

from api.enums import WorkflowRunMode
from api.services.pipecat.livekit_transfer_flow import (
    execute_cold_transfer,
    valid_destination,
)
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import Frame, InputDTMFFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

_DEBOUNCE_SECONDS = 0.5
_DEFAULT_FAILURE_MESSAGE = "抱歉，目前無法為您轉接，將由 AI 繼續為您服務。"


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
        self._failure_message = (
            config.get("transferFailedMessage") or _DEFAULT_FAILURE_MESSAGE
        )
        # S-L5-QUEUE gate health dimension; the whole tool config is handed
        # over — queue_is_healthy picks its own keys, unset means unchecked.
        self._queue_health_config = config
        self._unavailable_message = config.get("transferUnavailableMessage")
        self._unavailable_limit = config.get("unavailableAnnounceLimit")
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

        # Run the transfer off the frame loop — the SIP REFER is a multi-second
        # network round-trip and must not block frame processing.
        self.create_task(self._run_transfer(), f"{self}::press0_transfer")

    async def _run_transfer(self):
        try:
            result = await execute_cold_transfer(
                self._engine,
                room_name=self._room_name,
                destination=self._destination,
                schedule=self._schedule,
                after_hours_action=self._after_hours_action,
                alternate_destination=self._alternate_destination,
                after_hours_message=self._after_hours_message,
                transfer_reason="press0",
                queue_health_config=self._queue_health_config,
                unavailable_message=self._unavailable_message,
                unavailable_announce_limit=self._unavailable_limit,
            )
        except Exception:
            # this task dies silently inside the TaskManager — without this
            # belt the caller who pressed 0 gets no transfer, no message, and
            # no hangup (C4 dead-silence; review H1). Degrade to the same
            # spoken fallback as a structured failure.
            logger.exception("press-0 cold transfer crashed")
            result = {
                "status": "failed",
                "action": "transfer_failed",
                "reason": "internal_error",
            }
        logger.info(f"press-0 cold transfer result: {result}")

        # A genuine failure (e.g. REFER rejected) leaves the call up but with no
        # spoken feedback — unlike the voice path, DTMF has no LLM turn to inform
        # the caller. Announce a fallback so they are never left in silence (C4).
        # already_transferring is skipped: the concurrent trigger is handling it.
        if (
            result.get("status") == "failed"
            and result.get("reason") != "already_transferring"
        ):
            await self._engine.task.queue_frame(
                TTSSpeakFrame(self._failure_message, persist_to_logs=True)
            )


async def resolve_press0_gate(engine, workflow_run) -> Press0Gate | None:
    """Build the press-0 gate, or report why it could not be built.

    Split out of the pipeline setup so the not-installed paths are reachable in
    a test — everything around them needs a full transport/LLM build to reach.
    """
    if not workflow_run or workflow_run.mode != WorkflowRunMode.LIVEKIT.value:
        return None

    room_name = (workflow_run.initial_context or {}).get("room_name")
    transfer_config = await engine.resolve_transfer_call_config()
    destination = (transfer_config or {}).get("destination", "")

    if room_name and transfer_config and valid_destination(destination):
        return Press0Gate(engine, room_name=room_name, config=transfer_config)

    if transfer_config and not valid_destination(destination):
        # A configured-but-malformed destination is a deployment defect, not a
        # workflow choice: press-0 silently does nothing for the whole call and
        # no other path reports it (execute_cold_transfer only emits once the
        # caller has already asked to be transferred). Alert like any other
        # transfer failure (S-L7-OBS H2) — a workflow with no transfer tool at
        # all stays on the quiet info branch below.
        from api.services.observability.call_events import emit
        from api.services.observability.call_outcome import record_call_outcome

        emit(
            "transfer.failed",
            room_name=room_name or "",
            reason="press0_gate_not_installed",
            workflow_run_id=workflow_run.id,
            destination=destination,
            transfer_reason="press0",
        )
        # The event alerts, but annotations are the queryable layer: without
        # this the run falls through to the unconditional ai_completed at the
        # end of the pipeline and a whole misconfigured deployment reads as
        # clean AI completions. Rank 1 lets a later successful voice transfer
        # still win — the precedence call_outcome documents for exactly this
        # "failed press-0 followed by a successful voice transfer" sequence.
        #
        # Distinct suffix from execute_cold_transfer's "transfer_failed:press0"
        # (which means the caller pressed 0 and the REFER was refused). Sharing
        # the string would defeat the point of writing it: the queryable layer
        # could not tell "the gate never installed" from "the transfer failed".
        await record_call_outcome(
            engine,
            workflow_run.id,
            outcome="transfer_failed:press0_not_installed",
            transfer_reason="press0",
        )
        return None

    logger.info("press-0 gate not installed (no room_name or no transfer_call tool)")
    return None
