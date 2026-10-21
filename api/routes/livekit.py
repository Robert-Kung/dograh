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

``POST /livekit/did-mappings`` is the deployment-bootstrap path that binds a
DID to an AI workflow; see ``livekit_did_mapping`` for why it needs a
``provider='livekit'`` FK-container config (stop-gap until S-L6-ROUTING).
"""

import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from api.db import db_client
from api.db.models import UserModel
from api.schemas.livekit_did_mapping import (
    LiveKitDidMappingRequest,
    LiveKitDidMappingResponse,
)
from api.services.auth.depends import get_user
from api.services.pipecat.livekit_did_mapping import (
    ConcurrentDidMappingError,
    ContainerConfigAmbiguousError,
    InvalidDidAddressError,
    WorkflowNotInOrgError,
    ensure_livekit_did_mapping,
)
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


_DID_MAPPING_STATUS_CODES = {"created": 201, "conflict": 409}


@router.post(
    "/did-mappings",
    response_model=LiveKitDidMappingResponse,
    responses={
        201: {"model": LiveKitDidMappingResponse, "description": "Created"},
        409: {
            "model": LiveKitDidMappingResponse,
            "description": "Number bound elsewhere (status=conflict; "
            "current.provider tells whether overwrite can help)",
        },
        422: {
            "description": "Not a PSTN number, or the org's livekit "
            "container config is ambiguous (detail)"
        },
        503: {"description": "Concurrent write; retry"},
    },
)
async def ensure_did_mapping(
    request: LiveKitDidMappingRequest, user: UserModel = Depends(get_user)
):
    """Ensure a DID routes LiveKit inbound calls to an AI workflow (idempotent).

    201 ``created``, 200 ``unchanged`` / ``rebound``, 409 ``conflict`` (pass
    ``overwrite`` to rebind a number held by the livekit container; numbers of
    other provider configs are never moved).
    """
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")
    try:
        result = await ensure_livekit_did_mapping(
            organization_id=user.selected_organization_id,
            address=request.address,
            workflow_id=request.workflow_id,
            overwrite=request.overwrite,
        )
    except InvalidDidAddressError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except WorkflowNotInOrgError:
        raise HTTPException(status_code=404, detail="Workflow not found")
    except ContainerConfigAmbiguousError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ConcurrentDidMappingError as e:
        raise HTTPException(status_code=503, detail=str(e))

    body = LiveKitDidMappingResponse.model_validate(result, from_attributes=True)
    return JSONResponse(
        status_code=_DID_MAPPING_STATUS_CODES.get(result.status, 200),
        content=body.model_dump(mode="json"),
    )
