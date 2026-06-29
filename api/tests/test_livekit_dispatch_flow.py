"""Dispatch flow tests (S-L1-DISPATCH): C4 fallback on unresolved DID."""

import pytest

from api.services.pipecat.livekit_dispatcher import dispatch_livekit_call


@pytest.mark.asyncio
async def test_unmapped_did_routes_to_fallback():
    fb = {}

    async def resolver(did):
        return None

    async def fallback(room, reason):
        fb["room"], fb["reason"] = room, reason

    await dispatch_livekit_call("cs-+886912345678", resolver, fallback)
    assert fb == {"room": "cs-+886912345678", "reason": "unmapped_did"}


@pytest.mark.asyncio
async def test_no_did_routes_to_fallback():
    fb = {}

    async def resolver(did):
        raise AssertionError("resolver should not run without a DID")

    async def fallback(room, reason):
        fb["reason"] = reason

    await dispatch_livekit_call("garbage-room", resolver, fallback)
    assert fb["reason"] == "no_did"
