"""LiveKit SIP cold transfer (S-L3-COLDXFER).

Net-new branch per C3 — does NOT reuse CallTransferManager/TransferStrategy
(those are Twilio conference / call_sid shaped). Locates the SIP caller in the
room and hands them to a human queue via SIP REFER; the agent fully exits after
the bridge. Every failure path returns a structured result (C4), never raises.
"""

import asyncio
import os
from contextlib import asynccontextmanager

from livekit import api
from livekit.protocol.models import ParticipantInfo
from livekit.protocol.room import ListParticipantsRequest
from livekit.protocol.sip import TransferSIPParticipantRequest

SIP_KIND = ParticipantInfo.Kind.SIP

WAIT_SIP_ATTEMPTS = 6
WAIT_SIP_INTERVAL_SECONDS = 0.5


@asynccontextmanager
async def livekit_api(lk: api.LiveKitAPI | None = None):
    """Yield ``lk`` as-is, or construct a client from env and close it after."""
    if lk is not None:
        yield lk
        return
    own = api.LiveKitAPI(
        url=os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        yield own
    finally:
        await own.aclose()


async def wait_for_sip_participant(
    room_name: str,
    *,
    lk: api.LiveKitAPI | None = None,
    attempts: int | None = None,
    interval_seconds: float | None = None,
) -> str | None:
    """Short-poll until the room's SIP caller appears; return their identity.

    ``room_started`` fires before the SIP participant has necessarily joined,
    so REFER paths that start from the webhook (capacity overflow, dispatch
    safetynet) race the caller into the room. Bounded: ``attempts`` polls,
    ``interval_seconds`` apart, then None — callers take their explicit-end
    leg (C4). Returning the identity lets callers hand it straight to
    :func:`cold_transfer_to_human`, avoiding a second list and its TOCTOU.
    """
    if attempts is None:
        attempts = WAIT_SIP_ATTEMPTS
    if interval_seconds is None:
        interval_seconds = WAIT_SIP_INTERVAL_SECONDS
    async with livekit_api(lk) as client:
        for attempt in range(attempts):
            try:
                resp = await client.room.list_participants(
                    ListParticipantsRequest(room=room_name)
                )
            except Exception:
                resp = None
            if resp is not None:
                identity = next(
                    (p.identity for p in resp.participants if p.kind == SIP_KIND),
                    None,
                )
                if identity is not None:
                    return identity
            if attempt < attempts - 1:
                await asyncio.sleep(interval_seconds)
    return None


async def cold_transfer_to_human(
    room_name: str,
    human_destination: str,
    *,
    play_dialtone: bool = False,
    headers: dict[str, str] | None = None,
    lk: api.LiveKitAPI | None = None,
    participant_identity: str | None = None,
) -> dict:
    """Cold-transfer the room's SIP caller to a human queue via SIP REFER.

    ``human_destination`` is a config value (``tel:+886...`` or
    ``sip:queue@host``), never caller-supplied. ``headers`` are attached to
    the REFER (UUI attached-data, e.g. the S-L4 ticket correlation id);
    platform-generated values only. ``participant_identity`` skips the lookup
    when the caller already located the SIP participant (e.g. via
    :func:`wait_for_sip_participant`). Returns a structured result so the LLM
    can respond on failure instead of dropping the call.
    """
    try:
        async with livekit_api(lk) as client:
            identity = participant_identity
            if identity is None:
                resp = await client.room.list_participants(
                    ListParticipantsRequest(room=room_name)
                )
                identity = next(
                    (p.identity for p in resp.participants if p.kind == SIP_KIND),
                    None,
                )
            if identity is None:
                return {
                    "status": "failed",
                    "action": "transfer_failed",
                    "reason": "no_sip_caller",
                }
            await client.sip.transfer_sip_participant(
                TransferSIPParticipantRequest(
                    room_name=room_name,
                    participant_identity=identity,
                    transfer_to=human_destination,
                    play_dialtone=play_dialtone,
                    headers=headers or {},
                )
            )
            return {"status": "success", "action": "transferred"}
    except Exception as e:
        return {
            "status": "failed",
            "action": "transfer_failed",
            "reason": "sip_refer_error",
            "message": str(e),
        }
