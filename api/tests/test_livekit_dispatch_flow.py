"""Dispatch flow tests (S-L1-DISPATCH): C4 fallback on unresolved DID."""

import os
import sys
import types

import pytest

from api.services.pipecat.livekit_dispatcher import (
    _sign_agent_token,
    dispatch_livekit_call,
)


def test_agent_token_claims(monkeypatch):
    captured = {}

    class FakeGrants:
        def __init__(self, **kw):
            captured.update(kw)

    class FakeToken:
        def __init__(self, *a):
            pass

        def with_identity(self, ident):
            captured["identity"] = ident
            return self

        def with_grants(self, g):
            return self

        def to_jwt(self):
            return "jwt"

    fake = types.ModuleType("livekit")
    fake.api = types.SimpleNamespace(AccessToken=FakeToken, VideoGrants=FakeGrants)
    monkeypatch.setitem(sys.modules, "livekit", fake)
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")

    assert _sign_agent_token("cs-+886", "agent-1") == "jwt"
    assert captured["identity"] == "agent-1"
    assert captured["room"] == "cs-+886"
    assert captured["can_publish"] and captured["can_subscribe"]


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
