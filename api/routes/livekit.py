"""LiveKit inbound webhook route (S-L1-DISPATCH).

Net-new branch per C3. The SIP caller joining a ``cs-`` room triggers the
agent (livekit-event-wiring 設計 C); event routing, dedup and dispatch live in
``livekit_dispatcher``. This handler checks the dograh-only path secret,
verifies the LiveKit signature, and acks at once.

Path secret (security H-6 restated, H1): it only stops a *direct* POST from
forging a webhook. Anyone holding the LiveKit signing key who can reach the
media server — dograh and queue both can — can make the server emit genuine
events; that limit is a registered residual risk, not something this route
can close. Access logs mask the secret (``logging_config``).
"""

import os
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from api.db import db_client
from api.services.pipecat.livekit_dispatcher import (
    handle_webhook_event,
    record_webhook_rejected,
)

router = APIRouter(prefix="/livekit")

WEBHOOK_PATH_SECRET_ENV = "LIVEKIT_WEBHOOK_PATH_SECRET"


async def _did_resolver(did: str) -> tuple[int, int] | None:
    return await db_client.find_inbound_workflow_for_did(did)


async def _fallback(room_name: str, reason: str, workflow_run_id: int | None = None):
    # C4: never silent — REFER the caller to the fallback human queue, or end
    # the call explicitly. Runs in the background: the safetynet does SIP
    # REFER network I/O. Non-cs- rooms are logged and left alone.
    from api.services.pipecat.livekit_safetynet import server_side_safetynet, spawn

    return spawn(server_side_safetynet(room_name, reason, workflow_run_id))


def _verify(body: bytes, auth_header: str):
    from livekit import api

    receiver = api.WebhookReceiver(
        api.TokenVerifier(
            os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
        )
    )
    return receiver.receive(body.decode(), auth_header)


def _path_secret_matches(secret: str) -> bool:
    expected = os.environ.get(WEBHOOK_PATH_SECRET_ENV, "")
    if not expected:
        return False  # unset: the route does not exist
    return secrets.compare_digest(secret.encode(), expected.encode())


@router.post("/inbound/{secret}")
async def livekit_inbound(secret: str, request: Request):
    if not _path_secret_matches(secret):
        raise HTTPException(status_code=404)
    body = await request.body()
    auth = request.headers.get("Authorization", "")
    try:
        event = _verify(body, auth)
    except Exception as e:
        logger.warning(f"LiveKit webhook signature rejected: {type(e).__name__}")
        record_webhook_rejected(e)
        return JSONResponse(status_code=401, content={"ok": False})

    handle_webhook_event(event, _did_resolver, _fallback)
    return {"ok": True}
