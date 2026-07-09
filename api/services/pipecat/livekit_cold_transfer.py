"""LiveKit SIP cold transfer (S-L3-COLDXFER).

Net-new branch per C3 — does NOT reuse CallTransferManager/TransferStrategy
(those are Twilio conference / call_sid shaped). Locates the SIP caller in the
room and hands them to a human queue via SIP REFER; the agent fully exits after
the bridge. Every failure path returns a structured result (C4), never raises.
"""

import os
from contextlib import asynccontextmanager

from livekit import api
from livekit.protocol.models import ParticipantInfo
from livekit.protocol.room import ListParticipantsRequest
from livekit.protocol.sip import TransferSIPParticipantRequest

SIP_KIND = ParticipantInfo.Kind.SIP


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


async def cold_transfer_to_human(
    room_name: str,
    human_destination: str,
    *,
    play_dialtone: bool = False,
    headers: dict[str, str] | None = None,
    lk: api.LiveKitAPI | None = None,
) -> dict:
    """Cold-transfer the room's SIP caller to a human queue via SIP REFER.

    ``human_destination`` is a config value (``tel:+886...`` or
    ``sip:queue@host``), never caller-supplied. ``headers`` are attached to
    the REFER (UUI attached-data, e.g. the S-L4 ticket correlation id);
    platform-generated values only. Returns a structured result so the LLM
    can respond on failure instead of dropping the call.
    """
    try:
        async with livekit_api(lk) as client:
            resp = await client.room.list_participants(
                ListParticipantsRequest(room=room_name)
            )
            identity = next(
                (p.identity for p in resp.participants if p.kind == SIP_KIND), None
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
