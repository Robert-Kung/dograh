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
``workflow_run_id``, not the room name: rules are required to randomize
room names (``cs-_<dialed>_<random>``), but a room-name key would poison a
phone number after its first incident the day one does not. Pre-run failures
(``no_did``/``unmapped_did``) have no run id and skip the latch; their
redelivery is absorbed upstream by the dispatcher's per-call dedup. The latch is per-process by design: every
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
from api.services.observability.call_outcome import (
    record_call_fact,
    record_call_outcome,
)
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
    first failed call. Shape *and* premium-rate are both checked here: this is
    the only enforcement point that sees ``SAFETYNET_FALLBACK_QUEUE`` itself
    rather than whatever ``overflow_transfer_to()`` resolves to.
    """
    queue = fallback_queue()
    if queue is not None:
        if not valid_destination(queue):
            raise RuntimeError(
                f"SAFETYNET_FALLBACK_QUEUE {queue!r} is not tel:+E164 or sip:user@host"
            )
        # Premium-rate guard on the fallback queue itself, not only on the
        # capacity-overflow target. ``validate_capacity_config`` checks the
        # *effective* overflow destination, and ``overflow_transfer_to()``
        # returns the explicit value verbatim when it is set — so with
        # CAPACITY_OVERFLOW_TRANSFER_TO configured, this value never reached
        # any premium check at any enforcement point, while the dispatch-face
        # safetynet auto-REFERs every caller to it with nobody watching.
        # Imported from capacity_gate so the prefix list and its three-step
        # normalisation stay single-sourced (see the note there); local import
        # keeps the module-level import graph one-directional.
        from api.services.pipecat.capacity_gate import (
            PREMIUM_RATE_PREFIXES,
            _premium_rate,
        )

        if _premium_rate(queue):
            raise RuntimeError(
                f"SAFETYNET_FALLBACK_QUEUE {queue!r} matches a premium-rate "
                f"prefix {PREMIUM_RATE_PREFIXES}; refusing to boot"
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
        safetynet_event=event,  # published bind key from S-L3-SAFETYNET — keep
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


async def delete_room(room_name: str, lk) -> None:
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
            wait_for_sip_participant,
        )

        async with livekit_api(lk) as client:
            destination = fallback_queue()
            if destination is not None:
                # Bounded wait for the SIP caller (the reconciler and crash
                # paths do not start from its join event), then REFER with the
                # found identity (no second list, no TOCTOU).
                identity = await wait_for_sip_participant(room_name, lk=client)
                if identity is None:
                    result = {"status": "failed", "reason": "no_sip_caller"}
                else:
                    result = await cold_transfer_to_human(
                        room_name,
                        destination,
                        lk=client,
                        participant_identity=identity,
                    )
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

            await delete_room(room_name, client)
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

        await _interrupt_stalled_turn(engine)

        destination = fallback_queue()
        if destination is None:
            # The resolver never raises (ccp#7 D4) — a failed lookup comes
            # back as None, already reported, and the blank destination below
            # makes execute_cold_transfer report the refused transfer.
            config = await engine.resolve_transfer_call_config()
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
        # The announcement and the EndFrame enter at the pipeline source and
        # queue behind whatever stalled the bot — the usual reason this path
        # runs at all is a hung LLM stream. Measured (livekit-event-wiring
        # 5.3): the caller sat in silence for the full 30 s stall before the
        # message and the BYE. Bound it: past the deadline the room is deleted
        # server-side (the SIP leg gets its BYE) and the pipeline cancelled.
        spawn(_force_end_after_deadline(engine, room_name, workflow_run_id))
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


async def _interrupt_stalled_turn(engine) -> None:
    """Cut whatever holds the floor before the safetynet speaks.

    The announcements are TTSSpeakFrames queued at the pipeline source; a
    stalled LLM stream (the usual cause of ``bot_silence``) blocks them in its
    processor. An upstream ``InterruptionWorkerFrame`` bypasses the push
    queue and becomes an ``InterruptionFrame`` inside the pipeline, which
    cancels the in-flight generation. Best effort: never raises.
    """
    try:
        from pipecat.frames.frames import InterruptionWorkerFrame
        from pipecat.processors.frame_processor import FrameDirection

        await engine.task.queue_frame(
            InterruptionWorkerFrame(), FrameDirection.UPSTREAM
        )
    except Exception as e:
        logger.warning(f"safetynet interruption skipped: {e}")


SAFETYNET_END_DEADLINE_SECONDS = 10.0
SAFETYNET_END_FORCED_EVENT = "safetynet.end_forced"


async def _force_end_after_deadline(
    engine, room_name: str, workflow_run_id: Optional[int]
) -> None:
    """Explicit end within a bound when the graceful end is stuck (C4)."""
    await asyncio.sleep(SAFETYNET_END_DEADLINE_SECONDS)
    task = getattr(engine, "task", None)
    if task is None or task.has_finished():
        return
    log_event(
        SAFETYNET_END_FORCED_EVENT,
        room_name=room_name,
        reason="graceful_end_stalled",
        workflow_run_id=workflow_run_id,
    )
    from api.services.pipecat.livekit_call_events import delete_room_at_run_end

    await delete_room_at_run_end(room_name, workflow_run_id)
    try:
        await task.cancel(reason="safetynet_end_deadline")
    except Exception as e:
        logger.warning(f"safetynet forced cancel failed for {room_name}: {e}")


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


async def resolve_safetynet_watchdog(
    engine, workflow_run
) -> "SafetynetWatchdog | None":
    """Build the mid-call watchdog for a LIVEKIT run, or say why it could not be.

    Split out of ``_run_pipeline_impl`` for the same reason as
    ``resolve_press0_gate``: the not-installed branch must be reachable in a
    test, and everything around the install point needs a full transport/LLM
    build to reach.

    The watchdog is C4's last line — fatal ErrorFrames and owed-reply silence
    have nothing else behind them — so "it did not install" is a fact that has
    to be visible per call (ccp#7 D3). It used to be an ``if safetynet_room:``
    with no else: no log, no event, no outcome. It is recorded here as a fact,
    not raised as an error: a LIVEKIT run without ``room_name`` is a defect in
    how the run was created (the dispatcher always writes one), the call
    itself still proceeds, and the annotation is what lets "this call had no
    safety net" be counted and traced back.

    Recorded as a **fact**, not as the call outcome (review gate #1): press-0
    hits the same condition on the same engine moments earlier and takes the
    rank-1 ``call_outcome`` slot, so a second rank-1 write here was dropped
    whenever the workflow had a transfer tool — the marker landed only on
    workflows *without* one. ``safetynet_installed=false`` under its own key
    lands unconditionally and survives whatever outcome the call then has
    (``ai_completed`` included: the call did complete, it just ran without a
    net). Key absent means installed; the normal path writes nothing.
    """
    from api.enums import WorkflowRunMode

    if not workflow_run or workflow_run.mode != WorkflowRunMode.LIVEKIT.value:
        return None

    room_name = (workflow_run.initial_context or {}).get("room_name")
    if not room_name:
        call_events.emit(
            "transfer.failed",
            room_name="",
            reason="safetynet_not_installed",
            workflow_run_id=workflow_run.id,
            transfer_reason="safetynet",
        )
        await record_call_fact(
            workflow_run.id,
            safetynet_installed=False,
            safetynet_not_installed_reason="no_room",
        )
        logger.warning(
            f"safetynet watchdog not installed: LIVEKIT run {workflow_run.id} has "
            f"no room_name in initial_context (deployment defect); the call runs "
            f"without the fatal-error / silence fallback"
        )
        return None

    async def _on_fatal(reason: str) -> None:
        await midcall_safetynet(
            engine,
            room_name=room_name,
            reason=reason,
            workflow_run_id=workflow_run.id,
        )

    return SafetynetWatchdog(
        on_fatal=_on_fatal, room_name=room_name, workflow_run_id=workflow_run.id
    )


# --- reconciliation of undispatched rooms (livekit-event-wiring 設計 C) -----

RECONCILE_INTERVAL_SECONDS = 30.0
RECONCILE_MIN_AGE_SECONDS = 15.0
RECONCILE_FAILED_EVENT = "livekit.reconcile_failed"


async def reconcile_undispatched_rooms(
    lk=None, *, now: float | None = None, dedup=None
) -> int:
    """Hand every ``cs-`` room whose SIP caller waits with no agent to the safetynet.

    Covers a lost dispatch webhook — dograh restarting or down when the caller
    joined, a path-secret or key mismatch, a rejected signature — which would
    otherwise leave the caller in a silent room (C4). A room qualifies when it
    has a SIP participant, no ``agent-*`` participant, the caller joined more
    than ``RECONCILE_MIN_AGE_SECONDS`` ago, and the call is not claimed by the
    dispatcher (in flight or done). Claimed here before the safetynet runs, so
    a late webhook cannot dispatch the same call concurrently. Returns how many
    rooms were handed off; LiveKit API errors propagate to the loop.
    """
    from livekit.protocol.room import ListParticipantsRequest, ListRoomsRequest

    from api.services.pipecat import livekit_dispatcher
    from api.services.pipecat.livekit_cold_transfer import SIP_KIND, livekit_api

    dedup = dedup or livekit_dispatcher.dispatch_dedup
    now = time.time() if now is None else now
    handed = 0
    async with livekit_api(lk) as client:
        rooms = (await client.room.list_rooms(ListRoomsRequest())).rooms
        for room in rooms:
            if not room.name.startswith(DEFAULT_ROOM_PREFIX):
                continue
            participants = (
                await client.room.list_participants(
                    ListParticipantsRequest(room=room.name)
                )
            ).participants
            if any(p.identity.startswith("agent-") for p in participants):
                continue
            caller = next((p for p in participants if p.kind == SIP_KIND), None)
            if caller is None or now - caller.joined_at < RECONCILE_MIN_AGE_SECONDS:
                continue
            key = livekit_dispatcher.dispatch_key(caller)
            if not dedup.claim(key):
                continue
            try:
                await server_side_safetynet(room.name, "undispatched", None, lk=client)
            except BaseException:
                dedup.release(key)
                raise
            dedup.commit(key)
            handed += 1
    return handed


async def run_reconciler(interval_seconds: float = RECONCILE_INTERVAL_SECONDS) -> None:
    """Reconcile at startup and every ``interval_seconds``; never raises.

    A failed listing affects neither dispatch nor live calls: it is reported
    as an event and the next round tries again.
    """
    while True:
        try:
            await reconcile_undispatched_rooms()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            call_events.emit(
                RECONCILE_FAILED_EVENT, room_name="", reason=type(e).__name__
            )
        await asyncio.sleep(interval_seconds)


def start_reconciler() -> Optional[asyncio.Task]:
    """Start the loop in the process that receives the webhooks.

    The dispatch dedup is per process and LiveKit only posts to :8000, so a
    reconciler in another uvicorn worker could not see in-flight dispatches.
    No LiveKit configured → nothing to reconcile.
    """
    import sys

    if not os.environ.get("LIVEKIT_URL"):
        return None
    argv = sys.argv
    if "--port" in argv and argv[argv.index("--port") + 1 :][:1] != ["8000"]:
        return None
    return spawn(run_reconciler())
