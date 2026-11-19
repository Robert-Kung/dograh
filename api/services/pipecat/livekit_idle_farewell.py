"""The LIVEKIT idle hangup says goodbye before the BYE (ccp idle-hangup-farewell, C4).

Upstream's second idle asks the LLM for a goodbye and queues the ``EndFrame``
right behind the request. The request travels to the end of the pipeline and
back before the LLM sees it; the ``EndFrame`` goes straight down and always
gets there first, so the caller is hung up on in silence (pinned in
``test_idle_hangup_farewell``). On a LIVEKIT call the second idle runs
:func:`end_idle_call` instead:

- a fixed goodbye through ``TTSSpeakFrame`` — never the LLM — and the call
  ends only once the output transport reports it played, or the playback
  timeouts say it will not;
- while this runs, the safetynet watchdog drops its triggers
  (``CallFailureState.idle_end_in_progress``): the caller never spoke, so
  there is nobody to transfer, and a transfer racing the hangup would leave
  the exit to scheduling order;
- a goodbye that did not play is reported
  (``safetynet.idle_farewell_unplayed``) and the call ends all the same;
- the caller speaking before the goodbye starts calls the hangup off;
- the end itself is bounded from the moment it is decided:
  ``end_call_with_reason`` first runs a variable extraction with no timeout,
  and its ``EndFrame`` can queue behind a stuck TTS.

Applies to cascaded LIVEKIT calls with the watchdog installed
(:func:`idle_farewell_applies`); every other call keeps upstream's behaviour.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Optional

from loguru import logger

from api.services.pipecat import livekit_safetynet as sn
from api.services.pipecat.livekit_failure_exits import (
    IDLE_FAREWELL_TEXT,
    CallFailureState,
    PlaybackOutcome,
)
from api.utils.background import spawn
from pipecat.frames.frames import CancelFrame, InterruptionFrame, TTSSpeakFrame
from pipecat.utils.enums import EndTaskReason

# From the decision to end until the call must be over. Not from the idle
# event: a healthy TTS's first audio plus the goodbye itself is longer than
# this, and the steps before the decision carry their own bounds.
IDLE_END_DEADLINE_SECONDS = 5.0

IDLE_FAREWELL_UNPLAYED_EVENT = "safetynet.idle_farewell_unplayed"

_IDLE_REASON = EndTaskReason.USER_IDLE_MAX_DURATION_EXCEEDED.value


def idle_farewell_applies(exits) -> bool:
    """Cascaded LIVEKIT call with the watchdog installed.

    ``state`` exists only on a non-realtime LIVEKIT run (a realtime pipeline
    has no TTS processor to speak a ``TTSSpeakFrame``); without the watchdog
    nothing feeds the playback tracker and every wait would time out.
    """
    return exits.state is not None and exits.watchdog is not None


def idle_farewell_in_progress(exits) -> bool:
    return exits.state is not None and exits.state.idle_end_in_progress


def _transfer_in_progress(engine) -> bool:
    return getattr(engine, "_livekit_transfer_in_progress", False) is True


async def _cut_current_turn(engine) -> None:
    """Interrupt whatever holds the floor, ahead of the next frame queued.

    Straight into the pipeline, so that a frame queued after this call enters
    behind the interruption. ``livekit_safetynet._interrupt_stalled_turn``
    sends it by way of the worker's upstream queue instead, and it then lands
    after a ``TTSSpeakFrame`` queued right behind it — cutting the goodbye
    itself (measured in test_idle_hangup_farewell's harness). Best effort:
    never raises.
    """
    try:
        await engine.task._pipeline.queue_frame(InterruptionFrame())
    except Exception as e:
        logger.warning(f"idle farewell interruption skipped: {e}")


async def _play_farewell(
    engine, state: CallFailureState
) -> tuple[Optional[PlaybackOutcome], Optional[str]]:
    """Speak the goodbye; the playback outcome, or why it was not attempted."""
    if state.tts_unavailable:
        return None, "tts_unavailable"
    tracker = state.playback
    await tracker.wait_idle(state.playback_gap_timeout)
    # A stalled LLM stream (the idle prompt cut off mid-sentence) would hold
    # the goodbye back in its processor.
    await _cut_current_turn(engine)
    started_mark, user_mark = tracker.started, tracker.user_spoke
    await engine.task.queue_frame(
        TTSSpeakFrame(IDLE_FAREWELL_TEXT, persist_to_logs=True)
    )
    outcome = await tracker.wait_played(
        started_mark=started_mark,
        user_mark=user_mark,
        start_timeout=state.playback_start_timeout,
        gap_timeout=state.playback_gap_timeout,
        abort=lambda: state.tts_unavailable,
    )
    if outcome is PlaybackOutcome.TIMED_OUT:
        started = tracker.started > started_mark
        return outcome, "playback_stalled" if started else "start_timeout"
    if outcome is PlaybackOutcome.ABORTED:
        return outcome, "tts_unavailable"
    return outcome, None


async def end_idle_call(engine, exits, *, resume: Callable[[], None]) -> None:
    """End a LIVEKIT call whose caller stayed silent, goodbye first.

    Args:
        engine: The call's engine.
        exits: The call's ``LiveKitFailureExits``; :func:`idle_farewell_applies`
            must hold.
        resume: Called when the caller spoke up before the goodbye started
            and the call goes on (resets the idle count).
    """
    if _transfer_in_progress(engine):
        # A press-0 / voice transfer owns the call's exit.
        return
    state: CallFailureState = exits.state
    room_name = exits.watchdog.room_name
    workflow_run_id = exits.watchdog.workflow_run_id
    started = time.monotonic()

    state.idle_end_in_progress = True
    try:
        outcome, unplayed = await _play_farewell(engine, state)
    except Exception as e:
        # pipecat swallows an event handler's exception; without this the
        # flag would stay set on a call nothing is ending.
        logger.exception(f"idle farewell failed for {room_name}: {e}")
        outcome, unplayed = None, "flow_error"

    if outcome is PlaybackOutcome.INTERRUPTED:
        # VAD alone does not interrupt the pipeline: without this the queued
        # goodbye would still announce the end of a call that goes on.
        await _cut_current_turn(engine)
        state.idle_end_in_progress = False
        resume()
        return

    # From here to the spawn below nothing may await: the decision and the
    # start of its deadline are one step.
    if _transfer_in_progress(engine) or engine.is_call_disposed():
        # The caller pressed 0 during the goodbye, or the call is already
        # ending (the caller hung up): that path owns the exit, and the
        # watchdog is what bounds it.
        state.idle_end_in_progress = False
        return
    if unplayed is not None:
        if outcome is PlaybackOutcome.TIMED_OUT:
            state.mark_tts_unavailable("idle farewell playback timed out")
        sn.log_event(
            IDLE_FAREWELL_UNPLAYED_EVENT,
            room_name=room_name,
            reason=unplayed,
            workflow_run_id=workflow_run_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    end_task = asyncio.create_task(engine.end_call_with_reason(_IDLE_REASON))
    spawn(_force_idle_end_after_deadline(engine, end_task, room_name, workflow_run_id))
    # wait(), not await: the deadline cancels end_task, and that must not
    # read as this handler being cancelled.
    await asyncio.wait({end_task})
    if not end_task.cancelled() and end_task.exception() is not None:
        logger.error(f"idle end failed for {room_name}: {end_task.exception()!r}")


async def _force_idle_end_after_deadline(
    engine, end_task: asyncio.Task, room_name: str, workflow_run_id: Optional[int]
) -> None:
    """End the call when the graceful end has not finished in time (C4).

    Unlike ``livekit_safetynet._force_end_after_deadline`` this one owns the
    end task: an extraction on a dead LLM never returns, the event handler
    awaiting it never returns either, and the worker's cleanup waits for the
    handler — a ``CancelFrame`` alone would not end the run.
    """
    await asyncio.sleep(IDLE_END_DEADLINE_SECONDS)
    task = getattr(engine, "task", None)
    if task is None or task.has_finished():
        return
    end_task.cancel()
    # The cancel lands between "disposed" and "EndFrame queued":
    # end_call_with_reason cannot be called again to finish the job.
    engine.record_call_disposition(_IDLE_REASON)
    sn.log_event(
        sn.SAFETYNET_END_FORCED_EVENT,
        room_name=room_name,
        reason="idle_end_stalled",
        workflow_run_id=workflow_run_id,
    )
    from api.services.pipecat.livekit_call_events import delete_room_at_run_end

    await delete_room_at_run_end(room_name, workflow_run_id)
    try:
        # Both ways in: the worker's push queue when no EndFrame was queued
        # yet (the stuck extraction), straight into the pipeline when one was
        # and is itself what blocks that queue (a stuck TTS).
        await task.cancel(reason="idle_end_deadline")
        await task._pipeline.queue_frame(CancelFrame(reason="idle_end_deadline"))
    except Exception as e:
        logger.warning(f"idle forced cancel failed for {room_name}: {e}")
