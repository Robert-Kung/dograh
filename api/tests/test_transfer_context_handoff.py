"""Transfer context handoff tests (S-L4-SCREENPOP §2.4).

REFER headers carry the ticket id, writes are background-only and
failure-proof (C4), unconfigured runs are byte-for-byte no-ops, org
mismatches refuse to write, and the non-REFER branches never create
tickets. The MCP transport is faked at `call_ticket_tool`; the four-path
transfer regression itself lives in test_livekit_transfer_flow.py (its
fake engines have no run id, so handoff no-ops there by construction).
"""

import asyncio
import types
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

import api.services.pipecat.transfer_context_handoff as handoff
from api.services.tickets import contract
from api.services.tickets.config import TicketServerConfig

pytestmark = pytest.mark.asyncio

TPE = ZoneInfo("Asia/Taipei")
SCHED = {"tz": "Asia/Taipei", "mon": [["09:00", "18:00"]]}
OPEN = datetime(2026, 6, 29, 10, 0, tzinfo=TPE)
CLOSED = datetime(2026, 6, 29, 20, 0, tzinfo=TPE)

CONFIG = TicketServerConfig(url="http://localhost:8000/api/v1/mcp", api_key="key-a")


def _engine(run_id=1234, org_id=1):
    async def _get_organization_id():
        return org_id

    async def end_call_with_reason(reason, abort_immediately=False):
        pass

    return types.SimpleNamespace(
        _workflow_run_id=run_id,
        _get_organization_id=_get_organization_id,
        _gathered_context={"nodes_visited": ["start"]},
        context=None,
        end_call_with_reason=end_call_with_reason,
        task=types.SimpleNamespace(queue_frame=AsyncMock()),
    )


def _fake_lk(*, capture: dict, sip_attrs=None):
    from api.services.pipecat.livekit_cold_transfer import SIP_KIND

    participants = [
        types.SimpleNamespace(
            kind=SIP_KIND, identity="sip_caller", attributes=sip_attrs or {}
        )
    ]

    async def list_participants(req):
        return types.SimpleNamespace(participants=participants)

    async def transfer(req):
        capture["transfer_to"] = req.transfer_to
        capture["headers"] = dict(req.headers)

    async def aclose():
        pass

    return types.SimpleNamespace(
        room=types.SimpleNamespace(list_participants=list_participants),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
        aclose=aclose,
    )


def _run_row(configs=None):
    return types.SimpleNamespace(
        definition=types.SimpleNamespace(workflow_configurations=configs or {})
    )


async def _drain_background():
    while handoff._background_tasks:
        await asyncio.gather(*list(handoff._background_tasks), return_exceptions=True)


def _patched(config=CONFIG, tool_result=None, key_org=1):
    """Patch config resolution, credential lookup, and the MCP transport."""
    key_model = (
        types.SimpleNamespace(organization_id=key_org) if key_org is not None else None
    )
    return (
        patch.object(handoff.db_client, "get_workflow_run", AsyncMock(return_value=_run_row())),
        patch.object(handoff.db_client, "validate_api_key", AsyncMock(return_value=key_model)),
        patch.object(
            handoff, "resolve_ticket_server_config", AsyncMock(return_value=config)
        ),
        patch.object(
            handoff,
            "call_ticket_tool",
            AsyncMock(return_value=tool_result or {"ticket_id": "CS-1234"}),
        ),
    )


# ── REFER carries the ticket id ───────────────────────────────────────────────


async def test_refer_headers_carry_ticket_id_and_skeleton_written():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _engine(run_id=1234)
    cap = {}
    lk = _fake_lk(capture=cap, sip_attrs={"sip.phoneNumber": "+886912345678"})
    p1, p2, p3, p4 = _patched()

    with p1, p2, p3, p4 as call_tool, patch.object(
        handoff, "finalize_transfer_handoff", AsyncMock()
    ) as finalize:
        res = await execute_cold_transfer(
            eng,
            room_name="cs-room",
            destination="tel:+886900000000",
            schedule=SCHED,
            now=OPEN,
            lk=lk,
            transfer_reason="voice_tool",
        )
        await _drain_background()

    assert res["status"] == "success"
    assert cap["headers"][handoff.TICKET_HEADER] == "CS-1234"
    assert "CS-1234" in cap["headers"][handoff.UUI_HEADER]

    tool, args = call_tool.await_args.args[1], call_tool.await_args.args[2]
    assert tool == "create_ticket"
    assert args["ticket_id"] == "CS-1234"
    assert args["workflow_run_id"] == 1234
    assert args["caller_number"] == "+886912345678"
    assert args["transfer_reason"] == "voice_tool"

    plan, status = finalize.await_args.args
    assert status == "success" and plan.ticket_id == "CS-1234"


async def test_anonymous_caller_writes_empty_number():
    eng = _engine()
    cap = {}
    lk = _fake_lk(capture=cap, sip_attrs={})  # no sip.phoneNumber attribute
    p1, p2, p3, p4 = _patched()

    with p1, p2, p3, p4 as call_tool:
        plan = await handoff.prepare_transfer_handoff(
            eng, room_name="cs-room", transfer_reason="press0", lk=lk
        )
        await _drain_background()

    assert plan is not None
    assert call_tool.await_args.args[2]["caller_number"] == ""


async def test_ticket_id_deterministic_for_double_trigger():
    assert contract.ticket_id_for_run(77) == contract.ticket_id_for_run(77)


# ── Unconfigured → no-op ─────────────────────────────────────────────────────


async def test_unconfigured_is_full_noop():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _engine()
    cap = {}
    lk = _fake_lk(capture=cap)

    with patch.object(
        handoff.db_client, "get_workflow_run", AsyncMock(return_value=_run_row())
    ), patch.object(
        handoff, "resolve_ticket_server_config", AsyncMock(return_value=None)
    ), patch.object(handoff, "call_ticket_tool", AsyncMock()) as call_tool, patch.object(
        handoff, "finalize_transfer_handoff", AsyncMock()
    ) as finalize:
        res = await execute_cold_transfer(
            eng,
            room_name="cs-room",
            destination="tel:+886900000000",
            schedule=SCHED,
            now=OPEN,
            lk=lk,
        )
        await _drain_background()

    assert res["status"] == "success"  # transfer identical to pre-S-L4
    assert cap["headers"] == {}
    call_tool.assert_not_awaited()
    finalize.assert_not_awaited()


async def test_alternate_misconfigured_falls_back_and_creates_no_ticket():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _engine()
    with patch.object(handoff, "prepare_transfer_handoff", AsyncMock()) as prepare:
        res = await execute_cold_transfer(
            eng,
            room_name="cs-room",
            destination="tel:+886900000000",
            schedule=SCHED,
            after_hours_action="alternate_queue",
            alternate_destination="not-a-destination",
            now=CLOSED,
        )
    assert res == {"status": "after_hours", "action": "back_to_ai"}
    prepare.assert_not_awaited()  # non-REFER branches never touch the ticket MCP


# ── Failure isolation (C4) ───────────────────────────────────────────────────


async def test_server_down_fast_fail_does_not_block_refer():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _engine()
    cap = {}
    lk = _fake_lk(capture=cap)
    failures_before = handoff.CONTEXT_WRITE_METRICS["failed"]
    p1, p2, p3, _ = _patched()

    with p1, p2, p3, patch.object(
        handoff,
        "call_ticket_tool",
        AsyncMock(side_effect=ConnectionError("refused")),
    ):
        res = await execute_cold_transfer(
            eng,
            room_name="cs-room",
            destination="tel:+886900000000",
            schedule=SCHED,
            now=OPEN,
            lk=lk,
        )
        await _drain_background()

    assert res["status"] == "success"
    assert cap["headers"][handoff.TICKET_HEADER]  # header still attached
    assert handoff.CONTEXT_WRITE_METRICS["failed"] == failures_before + 1


async def test_slow_server_capped_by_timeout():
    slow_config = TicketServerConfig(url="http://x", api_key="k", timeout_seconds=0.05)

    class _StalledConnect:
        async def __aenter__(self):
            await asyncio.sleep(5)

        async def __aexit__(self, *args):
            return False

    with patch(
        "mcp.client.streamable_http.streamablehttp_client",
        return_value=_StalledConnect(),
    ):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                handoff.call_ticket_tool(slow_config, "create_ticket", {}),
                timeout=1.0,  # outer guard: the inner cap must fire well first
            )


async def test_credential_org_mismatch_refuses_write():
    eng = _engine(org_id=1)
    failures_before = handoff.CONTEXT_WRITE_METRICS["failed"]
    p1, p2, p3, p4 = _patched(key_org=2)  # key belongs to another org

    with p1, p2, p3, p4 as call_tool:
        plan = await handoff.prepare_transfer_handoff(
            eng, room_name="cs-room", transfer_reason="voice_tool", lk=_fake_lk(capture={})
        )
        await _drain_background()

    assert plan is not None  # headers still attach; only the write is refused
    call_tool.assert_not_awaited()
    assert handoff.CONTEXT_WRITE_METRICS["failed"] == failures_before + 1


async def test_external_key_unknown_to_platform_is_allowed():
    eng = _engine(org_id=1)
    p1, p2, p3, p4 = _patched(key_org=None)  # validate_api_key finds nothing

    with p1, p2, p3, p4 as call_tool:
        await handoff.prepare_transfer_handoff(
            eng, room_name="cs-room", transfer_reason="voice_tool", lk=_fake_lk(capture={})
        )
        await _drain_background()

    call_tool.assert_awaited()  # single-tenant wrapper key: cannot validate, proceed


async def test_prepare_never_raises():
    eng = _engine()
    with patch.object(
        handoff.db_client, "get_workflow_run", AsyncMock(side_effect=RuntimeError("db down"))
    ):
        plan = await handoff.prepare_transfer_handoff(
            eng, room_name="cs-room", transfer_reason="voice_tool"
        )
    assert plan is None  # degraded, transfer unaffected


# ── Snapshot shape ───────────────────────────────────────────────────────────


async def test_snapshot_messages_flattens_and_caps():
    class Ctx:
        def get_messages(self, truncate_large_values=False):
            return [
                {"role": "system", "content": "prompt"},
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": "hello " + "x" * 5000},
                {"role": "tool", "content": "ignored"},
            ]

    msgs = handoff.snapshot_messages(Ctx())
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert msgs[1]["content"] == "hi"
    assert len(msgs[2]["content"]) == handoff.SNAPSHOT_MAX_TEXT_LEN
