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

The safetynet fires at most once per call: a sticky room-name latch shared by
every trigger path. It must NOT reuse ``_livekit_transfer_in_progress`` — that
flag is an in-progress guard that resets in ``finally``, which would allow a
retry loop after a failed transfer.

Structured ``safetynet.*`` events are the S-L7-OBS subscription contract.
"""

import asyncio
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable

from loguru import logger

from api.services.pipecat.livekit_transfer_flow import valid_destination
from pipecat.observers.base_observer import BaseObserver

CS_ROOM_PREFIX = "cs-"

_DEFAULT_MAX_SILENCE_SECONDS = 8.0
_ANNOUNCE_MESSAGE = "為您轉接專員，請稍候。"
_FAILURE_MESSAGE = "系統發生問題無法繼續服務，請稍後再撥，謝謝。"
_ANNOUNCE_TIMEOUT_SECONDS = 2.0

# Sticky once-per-call latch, keyed by room name (unique per call). Bounded:
# entries only accumulate when the safetynet fires, and the deque evicts the
# oldest rooms long after their calls have ended.
_fired_rooms: set[str] = set()
_fired_history: deque = deque(maxlen=1024)


def fallback_queue() -> str | None:
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
    workflow_run_id: int | None = None,
    elapsed_ms: int | None = None,
) -> None:
    """Emit one structured ``safetynet.*`` event (S-L7-OBS contract)."""
    logger.bind(
        safetynet_event=event,
        room_name=room_name,
        reason=reason,
        workflow_run_id=workflow_run_id,
        elapsed_ms=elapsed_ms,
    ).warning(
        f"{event} room={room_name} reason={reason} "
        f"workflow_run_id={workflow_run_id} elapsed_ms={elapsed_ms}"
    )


def claim(room_name: str) -> bool:
    """Claim the room's single safetynet shot; False if already fired."""
    if room_name in _fired_rooms:
        return False
    _fired_rooms.add(room_name)
    _fired_history.append(room_name)
    if len(_fired_rooms) > len(_fired_history):
        _fired_rooms.intersection_update(_fired_history)
    return True


async def _delete_room(room_name: str, lk=None) -> None:
    """Delete the room so the caller hears a hangup, never a silent room (C4)."""
    from livekit import api
    from livekit.protocol.room import DeleteRoomRequest

    own = lk is None
    if own:
        lk = api.LiveKitAPI(
            url=os.environ["LIVEKIT_URL"],
            api_key=os.environ["LIVEKIT_API_KEY"],
            api_secret=os.environ["LIVEKIT_API_SECRET"],
        )
    try:
        await lk.room.delete_room(DeleteRoomRequest(room=room_name))
    except Exception as e:
        logger.error(f"safetynet room delete failed for {room_name}: {e}")
    finally:
        if own:
            await lk.aclose()


async def server_side_safetynet(
    room_name: str,
    reason: str,
    workflow_run_id: int | None = None,
    lk=None,
) -> None:
    """Engine-free safetynet: REFER the room's SIP caller to the fallback queue.

    Used when no working agent is in the room — dispatch failures and pipeline
    crashes. Only ``cs-`` rooms are touched: every other room on the LiveKit
    project (tests, future outbound) is logged and left alone. Never raises.
    """
    if not room_name or not room_name.startswith(CS_ROOM_PREFIX):
        logger.warning(
            f"LiveKit dispatch fallback (non-{CS_ROOM_PREFIX} room, ignoring) "
            f"room={room_name} reason={reason}"
        )
        return
    if not claim(room_name):
        logger.info(f"safetynet already fired for {room_name}; skipping {reason}")
        return

    started = time.monotonic()
    log_event(
        "safetynet.triggered",
        room_name=room_name,
        reason=reason,
        workflow_run_id=workflow_run_id,
    )

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        destination = fallback_queue()
        if destination is not None:
            from api.services.pipecat.livekit_cold_transfer import (
                cold_transfer_to_human,
            )

            result = await cold_transfer_to_human(room_name, destination, lk=lk)
            if result.get("status") == "success":
                log_event(
                    "safetynet.transfer_ok",
                    room_name=room_name,
                    reason=reason,
                    workflow_run_id=workflow_run_id,
                    elapsed_ms=_elapsed(),
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

        await _delete_room(room_name, lk=lk)
        log_event(
            "safetynet.terminated",
            room_name=room_name,
            reason=reason,
            workflow_run_id=workflow_run_id,
            elapsed_ms=_elapsed(),
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
    workflow_run_id: int | None = None,
) -> None:
    """Fatal-condition transfer while the agent is (partially) alive.

    Announces best-effort (TTS may be dead — bounded, never blocking), then
    runs the shared cold-transfer flow with ``schedule=None`` so the transfer
    happens regardless of business hours. On failure announces an explicit
    message and ends the call; if even that raises (half-dead engine —
    ``execute_cold_transfer``'s never-raises contract doesn't survive one),
    falls back to the server-side path. Never raises.
    """
    if not claim(room_name):
        logger.info(f"safetynet already fired for {room_name}; skipping {reason}")
        return

    started = time.monotonic()
    log_event(
        "safetynet.triggered",
        room_name=room_name,
        reason=reason,
        workflow_run_id=workflow_run_id,
    )

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    destination = fallback_queue()
    if destination is None:
        config = None
        try:
            config = await engine.resolve_transfer_call_config()
        except Exception as e:
            logger.warning(f"safetynet could not resolve transfer config: {e}")
        destination = ((config or {}).get("destination") or "").strip()

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
    except Exception as e:
        logger.exception(f"mid-call safetynet failed for {room_name}: {e}")
        # The engine is too broken to end the call itself — server-side exit.
        _fired_rooms.discard(room_name)  # let the server-side path re-claim
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
    """

    def __init__(
        self,
        *,
        on_fatal: Callable[[str], Awaitable[None]],
        threshold_seconds: float | None = None,
        poll_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        max_frames: int = 100,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._on_fatal = on_fatal
        self._threshold = (
            threshold_seconds if threshold_seconds is not None else max_silence_seconds()
        )
        self._poll_seconds = poll_seconds
        self._clock = clock

        self._deadline: float | None = None
        self._pending_since: float | None = None  # reply owed since (survives tool calls)
        self._tool_calls: set[str] = set()
        self._fired = False
        self._monitor: asyncio.Task | None = None

        self._processed_frames: set = set()
        self._frame_history: deque = deque(maxlen=max_frames)

    async def on_push_frame(self, data) -> None:
        from pipecat.frames.frames import (
            BotStartedSpeakingFrame,
            ClientConnectedFrame,
            ErrorFrame,
            FunctionCallInProgressFrame,
            FunctionCallResultFrame,
            UserStoppedSpeakingFrame,
            VADUserStartedSpeakingFrame,
        )

        frame = data.frame
        if frame.id in self._processed_frames:
            return
        self._processed_frames.add(frame.id)
        self._frame_history.append(frame.id)
        if len(self._processed_frames) > len(self._frame_history):
            self._processed_frames = set(self._frame_history)

        if self._monitor is None:
            self._monitor = asyncio.create_task(self._run_monitor())

        # ErrorFrame travels upstream; everything else we care about is downstream.
        if isinstance(frame, ErrorFrame):
            if frame.fatal:
                await self._fire("fatal_error")
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
            if not self._tool_calls and self._pending_since is not None:
                self._deadline = self._clock() + self._threshold

    def _arm(self) -> None:
        self._pending_since = self._clock()
        if not self._tool_calls:
            self._deadline = self._pending_since + self._threshold

    def _disarm(self) -> None:
        self._pending_since = None
        self._deadline = None

    def due(self, now: float) -> bool:
        """True when the armed silence deadline has passed (test seam)."""
        return (
            not self._fired
            and self._deadline is not None
            and not self._tool_calls
            and now >= self._deadline
        )

    async def _fire(self, reason: str) -> None:
        if self._fired:
            return
        self._fired = True
        self._disarm()
        try:
            await self._on_fatal(reason)
        except Exception as e:
            logger.exception(f"safetynet watchdog callback failed: {e}")

    async def _run_monitor(self) -> None:
        while not self._fired:
            await asyncio.sleep(self._poll_seconds)
            if self.due(self._clock()):
                await self._fire("bot_silence")

    async def stop(self) -> None:
        self._fired = True
        if self._monitor is not None:
            self._monitor.cancel()
            try:
                await self._monitor
            except asyncio.CancelledError:
                pass
            self._monitor = None
