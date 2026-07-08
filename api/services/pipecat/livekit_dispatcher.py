"""LiveKit inbound dispatcher (S-L1-DISPATCH).

Resolves a LiveKit SIP inbound call into a Dograh agent run: parse DID,
resolve workflow, create a LIVEKIT workflow_run, sign an agent token, and
launch ``run_pipeline_livekit`` non-blockingly. Trigger wiring (webhook vs
explicit dispatch) is intentionally left to the route layer — open question.
"""

import asyncio
import os
from typing import Awaitable, Callable, Optional

from api.utils.telephony_address import normalize_telephony_address

DEFAULT_ROOM_PREFIX = "cs-"

# DID -> (workflow_id, user_id). Storage/owner is an open question
# (relates to S-L6-ROUTING); injected so this layer makes no assumption.
DidResolver = Callable[[str], Awaitable[Optional[tuple[int, int]]]]


def parse_did_from_room(
    room_name: str, prefix: str = DEFAULT_ROOM_PREFIX
) -> str | None:
    """Extract the dialed DID from a LiveKit room name like ``cs-+886...``."""
    if not room_name or not room_name.startswith(prefix):
        return None
    raw = room_name[len(prefix) :]
    try:
        normalized = normalize_telephony_address(raw)
    except ValueError:
        return None
    return normalized.canonical or None


def _sign_agent_token(room_name: str, identity: str) -> str:
    from livekit import api

    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )


async def dispatch_livekit_call(
    room_name: str,
    resolver: DidResolver,
    fallback: Callable[..., Awaitable[None]],
    livekit_url: str | None = None,
) -> None:
    """Resolve an inbound LiveKit call and launch the agent (non-blocking).

    Never fails silently (C4): unresolved DID or launch error routes to
    ``fallback(room_name, reason, workflow_run_id=None)``. Real launch
    failures happen inside the fire-and-forget pipeline task, so the task's
    done callback — not the synchronous ``try`` — is what routes them to
    ``fallback`` (S-L3-SAFETYNET).
    """
    did = parse_did_from_room(room_name)
    if not did:
        await fallback(room_name, "no_did")
        return

    resolved = await resolver(did)
    if not resolved:
        await fallback(room_name, "unmapped_did")
        return

    from loguru import logger

    from api.db import db_client
    from api.enums import CallType, WorkflowRunMode
    from api.services.pipecat.run_pipeline import run_pipeline_livekit

    workflow_id, user_id = resolved
    workflow_run = await db_client.create_workflow_run(
        name=f"livekit-{room_name}",
        workflow_id=workflow_id,
        mode=WorkflowRunMode.LIVEKIT.value,
        user_id=user_id,
        call_type=CallType.INBOUND,
        initial_context={"did": did, "room_name": room_name, "direction": "inbound"},
    )

    url = livekit_url or os.environ["LIVEKIT_URL"]
    token = _sign_agent_token(room_name, f"agent-{workflow_run.id}")

    def _on_pipeline_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        # Only reached when the pipeline's own safetynet failed to contain the
        # error — last-resort dispatch-face fallback.
        logger.opt(exception=exc).error(
            f"LiveKit pipeline task died for {room_name}: {exc}"
        )
        asyncio.create_task(fallback(room_name, "launch_failed", workflow_run.id))

    try:
        task = asyncio.create_task(
            run_pipeline_livekit(
                url=url,
                token=token,
                room_name=room_name,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.id,
                user_id=user_id,
            )
        )
        task.add_done_callback(_on_pipeline_done)
    except Exception as e:
        logger.exception(f"LiveKit pipeline launch failed for {room_name}: {e}")
        await fallback(room_name, "launch_failed", workflow_run.id)
