"""Per-call metadata written when a LIVEKIT inbound caller connects (ccp W4c 設計 C).

At the connect point the pipeline hands over the run id, room and the
instant recording started; everything else happens in a background task so
the call never waits on LiveKit or the database (C4). Failures are logged at
debug level without the number and never retried.

The caller number is taken only from a room with exactly one SIP participant
(two would mean a shared room and a possible mis-attribution), normalised by
``caller_identity`` and stored as mask, last 4 digits and HMAC — never clear.
"""

import asyncio
from datetime import datetime

from loguru import logger
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from api.db import db_client
from api.db.models import CcpCallMetaModel
from api.services.ccp import caller_identity as ci

_background: set[asyncio.Task] = set()


def on_connected(
    workflow_run_id: int, room_name: str, audio_started_at: datetime
) -> None:
    """Schedule the write; returns immediately and never raises (C4)."""
    try:
        task = asyncio.create_task(
            record_call_meta(workflow_run_id, room_name, audio_started_at)
        )
    except Exception as e:
        logger.debug(f"call meta: not scheduled: {type(e).__name__}")
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _sip_caller_numbers(room_name: str, lk=None) -> list[str]:
    """``sip.phoneNumber`` of every SIP participant in the room."""
    from livekit.protocol.room import ListParticipantsRequest

    from api.services.pipecat.livekit_cold_transfer import SIP_KIND, livekit_api

    async with livekit_api(lk) as client:
        resp = await client.room.list_participants(
            ListParticipantsRequest(room=room_name)
        )
    return [
        p.attributes.get("sip.phoneNumber", "")
        for p in resp.participants
        if p.kind == SIP_KIND
    ]


async def key_mismatch(session, key: bytes) -> bool:
    """Record the key's fingerprint on first use; True once it has changed."""
    fingerprint = ci.key_fingerprint(key)
    stored = (
        await session.execute(text("SELECT key_fingerprint FROM ccp_settings"))
    ).scalar_one_or_none()
    if stored is None:
        # first use only: a plain read every other call (no row lock per call)
        await session.execute(
            text(
                "INSERT INTO ccp_settings (id, key_fingerprint) VALUES (1, :fp)"
                " ON CONFLICT (id) DO UPDATE SET key_fingerprint = :fp"
                " WHERE ccp_settings.key_fingerprint IS NULL"
            ),
            {"fp": fingerprint},
        )
        stored = (
            await session.execute(text("SELECT key_fingerprint FROM ccp_settings"))
        ).scalar_one()
    return stored != fingerprint


def caller_columns(raw: str, key: bytes | None) -> dict:
    e164 = ci.normalize_caller(raw)
    if e164 is None or key is None:
        return {}
    return {
        "caller_masked": ci.mask(e164),
        "caller_last4": ci.last4(e164),
        "caller_hmac": ci.caller_hmac(key, e164),
    }


async def record_call_meta(
    workflow_run_id: int, room_name: str, audio_started_at: datetime, *, lk=None
) -> None:
    values = {"workflow_run_id": workflow_run_id, "audio_started_at": audio_started_at}
    try:
        numbers = await _sip_caller_numbers(room_name, lk)
    except Exception as e:
        numbers = []
        logger.debug(f"call meta: participant lookup failed: {type(e).__name__}")
    key = ci.hmac_key()
    if len(numbers) == 1:
        values.update(caller_columns(numbers[0], key))
    try:
        async with db_client.async_session() as session:
            if key is not None and "caller_hmac" in values:
                if await key_mismatch(session, key):
                    logger.error("call meta: CALLER_NUMBER_HMAC_KEY changed")
            await session.execute(
                insert(CcpCallMetaModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["workflow_run_id"])
            )
            await session.commit()
    except Exception as e:
        logger.debug(f"call meta: write failed: {type(e).__name__}")
