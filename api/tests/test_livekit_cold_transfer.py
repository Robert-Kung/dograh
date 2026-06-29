"""Cold transfer tests (S-L3-COLDXFER): four paths, structured failure (C4)."""

import types

import pytest

from api.services.pipecat.livekit_cold_transfer import (
    SIP_KIND,
    cold_transfer_to_human,
)


def _fake_lk(participants, *, raise_on_transfer=False):
    captured = {}

    async def list_participants(req):
        captured["room"] = req.room
        return types.SimpleNamespace(participants=participants)

    async def transfer(req):
        if raise_on_transfer:
            raise RuntimeError("provider rejected")
        captured["transfer_to"] = req.transfer_to
        captured["identity"] = req.participant_identity

    return types.SimpleNamespace(
        room=types.SimpleNamespace(list_participants=list_participants),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
    ), captured


@pytest.mark.asyncio
async def test_transfer_success():
    caller = types.SimpleNamespace(kind=SIP_KIND, identity="sip_abc")
    lk, cap = _fake_lk([caller])
    res = await cold_transfer_to_human("room1", "tel:+886912345678", lk=lk)
    assert res == {"status": "success", "action": "transferred"}
    assert cap["identity"] == "sip_abc"
    assert cap["transfer_to"] == "tel:+886912345678"


@pytest.mark.asyncio
async def test_no_sip_caller():
    web = types.SimpleNamespace(kind=0, identity="agent-1")
    lk, _ = _fake_lk([web])
    res = await cold_transfer_to_human("room1", "tel:+886912345678", lk=lk)
    assert res["status"] == "failed"
    assert res["reason"] == "no_sip_caller"


@pytest.mark.asyncio
async def test_refer_error_structured():
    caller = types.SimpleNamespace(kind=SIP_KIND, identity="sip_abc")
    lk, _ = _fake_lk([caller], raise_on_transfer=True)
    res = await cold_transfer_to_human("room1", "sip:queue@pbx", lk=lk)
    assert res["status"] == "failed"
    assert res["reason"] == "sip_refer_error"
    assert "provider rejected" in res["message"]


@pytest.mark.asyncio
async def test_caller_picked_over_agent():
    web = types.SimpleNamespace(kind=0, identity="agent-1")
    caller = types.SimpleNamespace(kind=SIP_KIND, identity="sip_abc")
    lk, cap = _fake_lk([web, caller])
    res = await cold_transfer_to_human("room1", "tel:+886912345678", lk=lk)
    assert res["status"] == "success"
    assert cap["identity"] == "sip_abc"
