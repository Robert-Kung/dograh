"""LiveKit failure safetynet (S-L3-SAFETYNET).

C4 (never dead air) for the infrastructure failure faces the voice-tool and
press-0 transfers don't cover:

- **Dispatch failures** (``no_did`` / ``unmapped_did`` / ``launch_failed``) and
  **pipeline crashes**: no (working) agent is in the room, so the caller is
  handed to the fallback human queue with a server-side SIP REFER via
  :func:`server_side_safetynet` — no engine involved. If that fails the room
  is deleted so the caller hears a hangup, never a silent room.
- **Mid-call fatal conditions** (fatal pipeline errors, the bot owing a reply
  and staying silent past the threshold): :class:`SafetynetWatchdog` observes
  the pipeline and :func:`midcall_safetynet` runs the shared cold-transfer
  flow with ``schedule=None`` — the business-hours gate is deliberately
  bypassed, because ``back_to_ai`` with a dead pipeline *is* dead air.

The safetynet fires at most once per call. The latch is keyed by
``workflow_run_id`` — room names repeat across calls (the SIP dispatch rule
templates them on the dialed DID: ``cs-{call.to}``), so a room-name key would
poison a phone number after its first incident. Pre-run failures
(``no_did``/``unmapped_did``) have no run id and no concurrent second trigger
path, so they skip the latch. The latch is per-process by design: every
trigger path for a given run (watchdog, crash catch-all, task done callback)
executes in the process that launched the pipeline. It must NOT reuse
``_livekit_transfer_in_progress`` — that flag is an in-progress guard that
resets in ``finally``, which would allow a retry loop after a failed transfer.

Structured ``safetynet.*`` events are the S-L7-OBS subscription contract.
"""

import asyncio
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Optional

from loguru import logger

from api.services.observability import call_events
from api.services.observability.call_outcome import record_call_outcome
from api.services.pipecat.livekit_dispatcher import DEFAULT_ROOM_PREFIX
from api.services.pipecat.livekit_transfer_flow import valid_destination
from api.utils.background import (
    spawn,  # noqa: F401 — re-export; callers import from here
)
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    ClientConnectedFrame,
    ErrorFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver

_DEFAULT_MAX_SILENCE_SECONDS = 8.0
_ANNOUNCE_MESSAGE = "為您轉接專員，請稍候。"
_FAILURE_MESSAGE = "系統發生問題無法繼續服務，請稍後再撥，謝謝。"
_ANNOUNCE_TIMEOUT_SECONDS = 2.0

_MAX_FIRED = 1024
_fired_runs: set[int] = set()
_fired_order: deque = deque()


def fallback_queue() -> Optional[str]:
    """The configured fallback human queue, or None when unset/blank."""
    value = (os.environ.get("SAFETYNET_FALLBACK_QUEUE") or "").strip()
    return value or None


def max_silence_seconds() -> float:
    return float(
        os.environ.get("SAFETYNET_MAX_SILENCE_SECONDS", _DEFAULT_MAX_SILENCE_SECONDS)
    )


def validate_safetynet_config() -> None:
    """Fail loudly at startup on malformed safetynet config (C6).

    The dispatch face has no engine and no call-time validation hook, so a bad
    fallback destination must stop the app from booting, not surface on the
    first failed call.
    """
    queue = fallback_queue()
    if queue is not None and not valid_destination(queue):
        raise RuntimeError(
            f"SAFETYNET_FALLBACK_QUEUE {queue!r} is not tel:+E164 or sip:user@host"
        )
    try:
        seconds = max_silence_seconds()
    except ValueError as e:
        raise RuntimeError(f"SAFETYNET_MAX_SILENCE_SECONDS is not a number: {e}") from e
    if seconds <= 0:
        raise RuntimeError(f"SAFETYNET_MAX_SILENCE_SECONDS must be > 0, got {seconds}")


def log_event(
    event: str,
    *,
    room_name: str,
    reason: str,
    workflow_run_id: Optional[int] = None,
    elapsed_ms: Optional[int] = None,
) -> None:
    """Emit one structured ``safetynet.*`` event (S-L7-OBS contract).

    Event names and fields are the published contract; delivery goes through
    the unified call-event path (structured log + alerting).
    """
    call_events.emit(
        event,
        room_name=room_name,
        reason=reason,
        workflow_run_id=workflow_run_id,
        elapsed_ms=elapsed_ms,
    )


def claim(workflow_run_id: Optional[int]) -> bool:
    """Claim the run's single safetynet shot; False if already fired.

    ``None`` (pre-run dispatch failures) always claims: those paths have no
    concurrent second trigger, and latching by room name would poison the DID
    for later calls.
    """
    if workflow_run_id is None:
        return True
    if workflow_run_id in _fired_runs:
        return False
    if len(_fired_order) >= _MAX_FIRED:
        _fired_runs.discard(_fired_order.popleft())
    _fired_runs.add(workflow_run_id)
    _fired_order.append(workflow_run_id)
    return True


def release(workflow_run_id: Optional[int]) -> None:
    """Undo a claim so another safetynet path can take over the same run."""
    if workflow_run_id is not None:
        _fired_runs.discard(workflow_run_id)


async def _delete_room(room_name: str, lk) -> None:
    """Delete the room so the caller hears a hangup, never a silent room (C4)."""
    from livekit.protocol.room import DeleteRoomRequest

    try:
        await lk.room.delete_room(DeleteRoomRequest(room=room_name))
    except Exception as e:
        logger.error(f"safetynet room delete failed for {room_name}: {e}")


async def server_side_safetynet(
    room_name: str,
    reason: str,
    workflow_run_id: Optional[int] = None,
    lk=None,
) -> None:
    """Engine-free safetynet: REFER the room's SIP caller to the fallback queue.

    Used when no working agent is in the room — dispatch failures and pipeline
    crashes. Only ``cs-`` rooms are touched: every other room on the LiveKit
    project (tests, future outbound) is logged and left alone. Never raises.
    """
    if not room_name or not room_name.startswith(DEFAULT_ROOM_PREFIX):
        logger.warning(
            f"LiveKit dispatch fallback (non-{DEFAULT_ROOM_PREFIX} room, ignoring) "
            f"room={room_name} reason={reason}"
        )
        return
    if not claim(workflow_run_id):
        logger.info(
            f"safetynet already fired for run {workflow_run_id}; skipping {reason}"
        )
        return

    started = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        log_event(
            "safetynet.triggered",
            room_name=room_name,
            reason=reason,
            workflow_run_id=workflow_run_id,
        )
        from api.services.pipecat.livekit_cold_transfer import (
            cold_transfer_to_human,
            livekit_api,
        )

        async with livekit_api(lk) as client:
            destination = fallback_queue()
            if destination is not None:
                result = await cold_transfer_to_human(room_name, destination, lk=client)
                if result.get("status") == "success":
                    log_event(
                        "safetynet.transfer_ok",
                        room_name=room_name,
                        reason=reason,
                        workflow_run_id=workflow_run_id,
                        elapsed_ms=_elapsed(),
                    )
                    await record_call_outcome(
                        None,
                        workflow_run_id,
                        outcome="transferred:safetynet",
                        transfer_reason="safetynet",
                    )
                    return
                log_event(
                    "safetynet.transfer_failed",
                    room_name=room_name,
                    reason=result.get("reason", "unknown"),
                    workflow_run_id=workflow_run_id,
                    elapsed_ms=_elapsed(),
                )
            else:
                logger.warning(
                    "SAFETYNET_FALLBACK_QUEUE not configured; ending call explicitly"
                )

            await _delete_room(room_name, client)
        log_event(
            "safetynet.terminated",
            room_name=room_name,
            reason=reason,
            workflow_run_id=workflow_run_id,
            elapsed_ms=_elapsed(),
        )
        await record_call_outcome(
            None,
            workflow_run_id,
            outcome="safetynet_terminated",
            transfer_reason="safetynet",
        )
    except Exception as e:
        # Last resort: the safetynet itself must never take the process down.
        logger.exception(f"server-side safetynet failed for {room_name}: {e}")
        log_event(
            "safetynet.transfer_failed",
            room_name=room_name,
            reason="safetynet_error",
            workflow_run_id=workflow_run_id,
            elapsed_ms=_elapsed(),
        )


async def midcall_safetynet(
    engine,
    *,
    room_name: str,
    reason: str,
    workflow_run_id: Optional[int] = None,
) -> None:
    """Fatal-condition transfer while the agent is (partially) alive.

    Announces best-effort (TTS may be dead — bounded, never blocking), then
    runs the shared cold-transfer flow with ``schedule=None`` so the transfer
    happens regardless of business hours. On failure announces an explicit
    message and ends the call; if even that raises (half-dead engine —
    ``execute_cold_transfer``'s never-raises contract doesn't survive one),
    falls back to the server-side path. Never raises.
    """
    if not claim(workflow_run_id):
        logger.info(
            f"safetynet already fired for run {workflow_run_id}; skipping {reason}"
        )
        return

    started = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    async def _announce(message: str) -> None:
        from pipecat.frames.frames import TTSSpeakFrame

        try:
            await asyncio.wait_for(
                engine.task.queue_frame(TTSSpeakFrame(message, persist_to_logs=True)),
                timeout=_ANNOUNCE_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.warning(f"safetynet announce skipped (TTS unavailable): {e}")

    try:
        log_event(
            "safetynet.triggered",
            room_name=room_name,
            reason=reason,
            workflow_run_id=workflow_run_id,
        )

        from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

        destination = fallback_queue()
        if destination is None:
            config = None
            try:
                config = await engine.resolve_transfer_call_config()
            except Exception as e:
                logger.warning(f"safetynet could not resolve transfer config: {e}")
            destination = ((config or {}).get("destination") or "").strip()

        result = await execute_cold_transfer(
            engine,
            room_name=room_name,
            destination=destination,
            schedule=None,  # bypass the business-hours gate: back_to_ai is dead air here
            before_refer=lambda: _announce(_ANNOUNCE_MESSAGE),
            transfer_reason="safetynet",
        )
        if result.get("status") == "success":
            log_event(
                "safetynet.transfer_ok",
                room_name=room_name,
                reason=reason,
                workflow_run_id=workflow_run_id,
                elapsed_ms=_elapsed(),
            )
            return
        if result.get("reason") == "already_transferring":
            # A voice/press-0 transfer is mid-flight; it owns the call's exit.
            logger.info(f"safetynet yielded to in-flight transfer for {room_name}")
            return
        log_event(
            "safetynet.transfer_failed",
            room_name=room_name,
            reason=result.get("reason", "unknown"),
            workflow_run_id=workflow_run_id,
            elapsed_ms=_elapsed(),
        )
        await _announce(_FAILURE_MESSAGE)
        from pipecat.utils.enums import EndTaskReason

        await engine.end_call_with_reason(
            EndTaskReason.PIPELINE_ERROR.value, abort_immediately=False
        )
        log_event(
            "safetynet.terminated",
            room_name=room_name,
            reason=reason,
            workflow_run_id=workflow_run_id,
            elapsed_ms=_elapsed(),
        )
        await record_call_outcome(
            engine,
            workflow_run_id,
            outcome="safetynet_terminated",
            transfer_reason="safetynet",
        )
    except Exception as e:
        logger.exception(f"mid-call safetynet failed for {room_name}: {e}")
        # The engine is too broken to end the call itself — server-side exit.
        release(workflow_run_id)
        await server_side_safetynet(
            room_name, "midcall_safetynet_error", workflow_run_id
        )


class SafetynetWatchdog(BaseObserver):
    """Pipeline observer detecting mid-call fatal conditions (S-L3-SAFETYNET).

    Fires ``on_fatal(reason)`` at most once when either:

    - a fatal ``ErrorFrame`` passes through the pipeline, or
    - the bot owes a reply (a user turn ended, or the call just connected and
      the greeting is due) and produces no speech within the threshold.

    The silence clock only runs while a reply is owed: it arms on
    ``UserStoppedSpeakingFrame`` / ``ClientConnectedFrame``, disarms on
    ``BotStartedSpeakingFrame`` or when the user starts speaking again, and is
    suspended while a function call is in flight — an MCP ticket lookup may
    legitimately hold the floor longer than the threshold (healthy calls must
    not be transferred).

    Frame handling is idempotent, so the same frame passing multiple processor
    hops needs no dedup. ``on_fatal`` runs in a background task: observers are
    awaited inline on the frame path, and the safetynet does announce + SIP
    REFER network I/O that must not stall frame processing.
    """

    def __init__(
        self,
        *,
        on_fatal: Callable[[str], Awaitable[None]],
        threshold_seconds: Optional[float] = None,
        poll_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        room_name: Optional[str] = None,
        workflow_run_id: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._on_fatal = on_fatal
        self._room_name = room_name
        self._workflow_run_id = workflow_run_id
        # An ErrorFrame is observed once per processor hop; dedup by frame id
        # so one provider error counts once in the alert window (S-L7-OBS).
        self._seen_errors: deque = deque(maxlen=100)
        self._threshold = (
            threshold_seconds
            if threshold_seconds is not None
            else max_silence_seconds()
        )
        self._poll_seconds = poll_seconds
        self._clock = clock

        self._deadline: Optional[float] = None
        self._reply_owed = False  # survives tool-call suspension
        self._tool_calls: set[str] = set()
        self._fired = False
        # Raw asyncio task on purpose: observers added via add_observer never
        # get setup(), so BaseObject.create_task has no task manager here.
        self._monitor: Optional[asyncio.Task] = None

    async def on_push_frame(self, data) -> None:
        frame = data.frame

        if self._monitor is None:
            self._monitor = asyncio.create_task(self._run_monitor())

        # ErrorFrame travels upstream; everything else we care about is downstream.
        if isinstance(frame, ErrorFrame):
            if frame.fatal:
                self._fire("fatal_error")
            elif frame.id not in self._seen_errors:
                self._seen_errors.append(frame.id)
                call_events.emit(
                    "provider.error",
                    room_name=self._room_name or "",
                    reason=str(frame.error)[:200],
                    workflow_run_id=self._workflow_run_id,
                )
            return

        if isinstance(frame, (ClientConnectedFrame, UserStoppedSpeakingFrame)):
            self._arm()
        elif isinstance(frame, (BotStartedSpeakingFrame, VADUserStartedSpeakingFrame)):
            self._disarm()
        elif isinstance(frame, FunctionCallInProgressFrame):
            self._tool_calls.add(frame.tool_call_id)
            self._deadline = None  # suspended, but the reply is still owed
        elif isinstance(frame, FunctionCallResultFrame):
            self._tool_calls.discard(frame.tool_call_id)
            if not self._tool_calls and self._reply_owed:
                self._deadline = self._clock() + self._threshold

    def _arm(self) -> None:
        self._reply_owed = True
        if not self._tool_calls:
            self._deadline = self._clock() + self._threshold

    def _disarm(self) -> None:
        self._reply_owed = False
        self._deadline = None

    def due(self, now: float) -> bool:
        """True when the armed silence deadline has passed (test seam)."""
        return (
            not self._fired
            and self._deadline is not None
            and not self._tool_calls
            and now >= self._deadline
        )

    def _fire(self, reason: str) -> None:
        if self._fired:
            return
        self._fired = True
        self._disarm()

        async def _run() -> None:
            try:
                await self._on_fatal(reason)
            except Exception as e:
                logger.exception(f"safetynet watchdog callback failed: {e}")

        spawn(_run())

    async def _run_monitor(self) -> None:
        while not self._fired:
            await asyncio.sleep(self._poll_seconds)
            if self.due(self._clock()):
                self._fire("bot_silence")

    async def stop(self) -> None:
        self._fired = True
        if self._monitor is not None:
            self._monitor.cancel()
            try:
                await self._monitor
            except asyncio.CancelledError:
                pass
            self._monitor = None
