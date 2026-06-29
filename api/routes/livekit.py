"""LiveKit inbound webhook route (S-L1-DISPATCH).

Net-new branch per C3: a single ``room_started`` webhook triggers the agent.
The handler verifies the LiveKit signature, returns immediately, and dispatches
non-blockingly via ``dispatch_livekit_call``. DID->workflow resolution reuses
the existing phone-number table as a stop-gap; the canonical store is owned by
S-L6-ROUTING. Any unresolved/failure path goes through ``fallback`` (C4).
"""

import os

from fastapi import APIRouter, Request
from loguru import logger

from api.db import db_client
from api.services.pipecat.livekit_dispatcher import dispatch_livekit_call

router = APIRouter(prefix="/livekit")


async def _did_resolver(did: str) -> tuple[int, int] | None:
    return await db_client.find_inbound_workflow_for_did(did)


async def _fallback(room_name: str, reason: str) -> None:
    # C4: never silent. A real transfer-to-human lands in S-L3-SAFETYNET;
    # here we record the reason so the call is observably routed, not dropped.
    logger.warning(f"LiveKit dispatch fallback room={room_name} reason={reason}")


def _verify(body: bytes, auth_header: str) -> bool:
    from livekit import api

    receiver = api.WebhookReceiver(
        api.AccessToken(
            os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
        )
    )
    receiver.receive(body.decode(), auth_header)
    return True


@router.post("/inbound")
async def livekit_inbound(request: Request):
    body = await request.body()
    auth = request.headers.get("Authorization", "")
    try:
        _verify(body, auth)
    except Exception as e:
        logger.warning(f"LiveKit webhook signature rejected: {e}")
        return {"ok": False}

    event = await request.json()
    if event.get("event") != "room_started":
        return {"ok": True}

    room_name = (event.get("room") or {}).get("name", "")
    await dispatch_livekit_call(room_name, _did_resolver, _fallback)
    return {"ok": True}
