"""LiveKit call lifecycle events (livekit-event-wiring 設計 A/B).

pipecat's ``LiveKitTransport`` does not declare ``on_client_connected`` /
``on_client_disconnected``: handlers registered under those names are dropped
with a warning (``BaseObject.add_event_handler``), so on LIVEKIT the opening,
recording notice, ``set_node`` and hangup handling never ran. This module maps
the shared "caller connected" / "caller hung up" actions onto the events the
LiveKit transport actually emits:

- ``on_first_participant_joined`` → caller connected (fires on connect when
  the caller is already in the room, else when they join). The SIP caller's
  participant sid is remembered here.
- ``on_participant_disconnected`` for *that* sid → caller hung up. Never
  "room is empty" (security L14): any other participant leaving is ignored.
  While a cold transfer is in flight (``engine._livekit_transfer_in_progress``)
  the hangup only marks ``engine._livekit_caller_left``; the transfer flow
  settles the call when it returns, so a REFER-accepted caller leg leaving
  first cannot rewrite the outcome to user hangup (C2).
- ``on_disconnected`` → the agent itself lost the media server: end the call
  instead of idling until the call cap.
- No remote participant ``CALLER_ABSENT_SECONDS`` after the agent connected →
  the caller hung up during dispatch: end the call (the run's finally deletes
  the room).

``on_call_state_updated`` is declared but never emitted — MUST NOT be used.
"""

import asyncio

from loguru import logger

CALLER_ABSENT_SECONDS = 10.0

# end_call_with_reason() reasons for the two LiveKit-only paths. Plain strings
# like the EndTaskReason values: they become ``call_disposition``.
CALLER_ABSENT_REASON = "caller_not_in_room"
AGENT_DISCONNECTED_REASON = "agent_disconnected"

ROOM_DELETE_FAILED_EVENT = "livekit.room_delete_failed"

# Every event name this module registers; the transport-contract test checks
# each against the real LiveKitTransport's declared set.
LIVEKIT_EVENT_NAMES = (
    "on_connected",
    "on_first_participant_joined",
    "on_participant_connected",
    "on_participant_disconnected",
    "on_disconnected",
)


def is_sip_participant(transport, participant_sid: str) -> bool:
    """True when ``participant_sid`` is a SIP participant in the transport's room.

    The transport exposes no kind accessor; ``remote_participants`` is keyed by
    identity, so match on sid. Any lookup failure reads as "not SIP".
    """
    from livekit import rtc

    try:
        participants = transport._client.room.remote_participants.values()
    except Exception:
        return False
    return any(
        p.sid == participant_sid and p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        for p in participants
    )


class LiveKitCallEvents:
    """Registers the LiveKit-side handlers for one call; see module docstring."""

    def __init__(
        self,
        transport,
        engine,
        *,
        on_caller_connected,
        on_caller_hangup,
        caller_absent_seconds: float = CALLER_ABSENT_SECONDS,
    ):
        self._transport = transport
        self._engine = engine
        self._on_caller_connected = on_caller_connected
        self._on_caller_hangup = on_caller_hangup
        self._caller_absent_seconds = caller_absent_seconds
        self.caller_sid: str | None = None
        self._absent_task: asyncio.Task | None = None

    def register(self) -> None:
        self._transport.add_event_handler("on_connected", self._on_connected)
        self._transport.add_event_handler(
            "on_first_participant_joined", self._on_first_participant_joined
        )
        self._transport.add_event_handler(
            "on_participant_connected", self._on_participant_connected
        )
        self._transport.add_event_handler(
            "on_participant_disconnected", self._on_participant_disconnected
        )
        self._transport.add_event_handler("on_disconnected", self._on_disconnected)

    def close(self) -> None:
        """Cancel the caller-absent timer (pipeline finished)."""
        if self._absent_task is not None and not self._absent_task.done():
            self._absent_task.cancel()

    async def _on_connected(self, _transport) -> None:
        # Not slept inside the handler: transport cleanup awaits every event
        # task (BaseObject.cleanup), so a sleeping handler would stall teardown.
        self._absent_task = asyncio.create_task(self._caller_absent_watch())

    async def _caller_absent_watch(self) -> None:
        await asyncio.sleep(self._caller_absent_seconds)
        if self.caller_sid is not None or self._engine.is_call_disposed():
            return
        if self._transport.get_participants():
            return
        logger.warning(
            f"LiveKit caller not in room {self._caller_absent_seconds:.0f}s "
            "after agent joined; ending call"
        )
        await self._engine.end_call_with_reason(
            CALLER_ABSENT_REASON, abort_immediately=True
        )

    async def _on_first_participant_joined(self, _transport, participant_sid) -> None:
        if is_sip_participant(self._transport, participant_sid):
            self.caller_sid = participant_sid
        else:
            logger.warning(
                f"first LiveKit participant {participant_sid} is not SIP; "
                "hangup detection disabled for this call"
            )
        await self._on_caller_connected()

    async def _on_participant_connected(self, _transport, participant_sid) -> None:
        # The first participant may not have been the caller (review D-10).
        if self.caller_sid is None and is_sip_participant(
            self._transport, participant_sid
        ):
            self.caller_sid = participant_sid

    async def _on_participant_disconnected(self, _transport, participant_sid) -> None:
        if self.caller_sid is None or participant_sid != self.caller_sid:
            return
        if getattr(self._engine, "_livekit_transfer_in_progress", False):
            # The transfer flow owns the ending (設計 B).
            self._engine._livekit_caller_left = True
            logger.info("SIP caller left during transfer; deferring to transfer flow")
            return
        await self._on_caller_hangup()

    async def _on_disconnected(self, _transport) -> None:
        if self._engine.is_call_disposed():
            return
        logger.warning("LiveKit agent disconnected from the room; ending call")
        await self._engine.end_call_with_reason(
            AGENT_DISCONNECTED_REASON, abort_immediately=True
        )


async def delete_room_at_run_end(
    room_name: str, workflow_run_id: int | None, lk=None
) -> None:
    """Delete the call's room once the LIVEKIT run ends (設計 F, review H1).

    The agent leaving the room does not end the SIP leg: without this, a call
    the AI ended (end-call node, safetynet failure message, idle/duration cap)
    left the caller listening to silence. Unconditional — after a successful
    transfer the caller leg is already gone and the delete is a no-op. The
    trunk's ``max_call_duration`` stays the backstop when the delete fails.
    Never raises.
    """
    from livekit.api.twirp_client import TwirpError
    from livekit.protocol.room import DeleteRoomRequest

    from api.services.observability.call_events import emit
    from api.services.pipecat.livekit_cold_transfer import livekit_api

    try:
        async with livekit_api(lk) as client:
            await client.room.delete_room(DeleteRoomRequest(room=room_name))
    except TwirpError as e:
        if e.code == "not_found":
            return  # already deleted by the safetynet/overflow path (review D-03)
        emit(
            ROOM_DELETE_FAILED_EVENT,
            room_name=room_name,
            reason=e.code or type(e).__name__,
            workflow_run_id=workflow_run_id,
        )
    except Exception as e:
        emit(
            ROOM_DELETE_FAILED_EVENT,
            room_name=room_name,
            reason=type(e).__name__,
            workflow_run_id=workflow_run_id,
        )
