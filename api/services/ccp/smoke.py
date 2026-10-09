"""Smoke-call recognition (local-deploy-sip-smoke design D2/D8).

A smoke call is recognised by ``ccp.smoke=1`` on the **SIP participant's**
attributes. Only a dispatch rule sets it (server side, by whoever holds the
LiveKit key); the caller cannot: trunk header mappings land under ``sip.h.*``
and the deployment checks forbid mapping any header into ``ccp.*``
(platform-deployment「`ccp.*` 屬性的不變式」). A missing attribute means a
production call -- counting more beats counting less.

The room is also registered in Redis so that every process (dispatcher,
safetynet, pipeline workers) can keep that call's alerts out of the on-call
webhook without threading the attribute through every emit site. A Redis
failure reads as "not smoke": alerts page rather than vanish.
"""

import os

from loguru import logger

SMOKE_ATTRIBUTE = "ccp.smoke"
# The run's tag. Written by the dispatcher on every LiveKit run (true or false)
# and stripped from every other source that merges into initial_context --
# workflow template variables, pre-call fetch results -- so neither an editor
# nor a CRM response can make a production call vanish from the reports.
RUN_CONTEXT_KEY = "ccp_smoke"
RESERVED_CONTEXT_KEYS = frozenset({RUN_CONTEXT_KEY})
ROOM_KEY_PREFIX = "ccp:smoke_room:"
# Longer than any call can last (trunk max_call_duration is 3600 s).
ROOM_TTL_SECONDS = 2 * 3600

_redis = None


def is_smoke(sip_attributes: dict | None) -> bool:
    return (sip_attributes or {}).get(SMOKE_ATTRIBUTE) == "1"


def without_reserved(context: dict | None) -> dict:
    """A merge source minus the dispatcher-owned keys."""
    return {k: v for k, v in (context or {}).items() if k not in RESERVED_CONTEXT_KEYS}


async def _get_redis():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(os.environ["REDIS_URL"])
    return _redis


async def mark_room(room_name: str) -> None:
    """Register a smoke room; never raises (a failure only means it may page)."""
    try:
        r = await _get_redis()
        await r.set(ROOM_KEY_PREFIX + room_name, "1", ex=ROOM_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"smoke room registration failed for {room_name}: {e}")


async def is_smoke_room(room_name: str | None) -> bool:
    if not room_name:
        return False
    try:
        r = await _get_redis()
        return bool(await r.exists(ROOM_KEY_PREFIX + room_name))
    except Exception as e:
        logger.warning(f"smoke room lookup failed for {room_name}: {e}")
        return False
