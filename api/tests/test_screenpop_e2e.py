"""Screen-pop end-to-end (S-L4-SCREENPOP §5.1, in-repo scope).

The whole handoff pipeline against a real contract server over real
streamable HTTP: transfer trigger → REFER (fake SIP leg, headers
captured) → skeleton ticket lands → summary job runs (canned LLM) →
summary + notes appended → screen-pop queries succeed by ticket id and
by caller number — plus the REFER-failure leg producing a
transfer_failed note instead of a summary.

Out of scope here, live-deferred with §0.2 (needs a SIP trunk): whether
the trunk forwards the UUI headers, and the LiveKit-stack room shape.
The compose-stack drill is documented in the change's preflight notes.
"""

import asyncio
import types
from unittest.mock import AsyncMock, patch

import pytest

import api.services.pipecat.transfer_context_handoff as handoff
import api.tasks.transfer_handoff as job_module
from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer
from api.services.tickets import contract
from api.services.tickets.config import TicketServerConfig
from api.services.tickets.reference_server import build_reference_mcp
from api.tasks.transfer_handoff import summarize_transfer_handoff
from api.tests.support.mcp_http_server import ThreadedMcpServer

pytestmark = pytest.mark.asyncio

CALLER = "+886955666777"
RUN_ID = 555001

CANNED_SUMMARY_JSON = (
    '{"intent": "dispute a double charge", '
    '"steps_done": ["confirmed the last invoice"], '
    '"pending": ["issue refund"], '
    '"transfer_reason": "caller asked for a human"}'
)


@pytest.fixture
def contract_server_url():
    with ThreadedMcpServer(build_reference_mcp()) as server:
        yield server.url


def _engine(run_id=RUN_ID, org_id=1):
    async def _get_organization_id():
        return org_id

    async def end_call_with_reason(reason, abort_immediately=False):
        pass

    class Ctx:
        def get_messages(self, truncate_large_values=False):
            return [
                {"role": "user", "content": "You double-charged me. I want a human."},
                {"role": "assistant", "content": "I'll transfer you to a colleague."},
            ]

    return types.SimpleNamespace(
        _workflow_run_id=run_id,
        _get_organization_id=_get_organization_id,
        _gathered_context={},
        context=Ctx(),
        end_call_with_reason=end_call_with_reason,
        task=types.SimpleNamespace(queue_frame=AsyncMock()),
    )


def _fake_lk(capture: dict, *, refer_fails=False):
    from api.services.pipecat.livekit_cold_transfer import SIP_KIND

    async def list_participants(req):
        return types.SimpleNamespace(
            participants=[
                types.SimpleNamespace(
                    kind=SIP_KIND,
                    identity="sip_caller",
                    attributes={"sip.phoneNumber": CALLER},
                )
            ]
        )

    async def transfer(req):
        if refer_fails:
            raise RuntimeError("REFER rejected by provider")
        capture["headers"] = dict(req.headers)
        capture["transfer_to"] = req.transfer_to

    async def aclose():
        pass

    return types.SimpleNamespace(
        room=types.SimpleNamespace(list_participants=list_participants),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
        aclose=aclose,
    )


def _wire(config: TicketServerConfig, enqueued: list):
    """Point the handoff at the real HTTP server; capture the ARQ enqueue."""
    run_row = types.SimpleNamespace(
        workflow_id=7,
        definition=types.SimpleNamespace(workflow_configurations={}),
    )

    async def fake_enqueue(function_name, *args):
        enqueued.append((function_name, args))

    return (
        patch.object(handoff.db_client, "get_workflow_run", AsyncMock(return_value=run_row)),
        patch.object(handoff.db_client, "validate_api_key", AsyncMock(return_value=None)),
        patch.object(handoff, "resolve_ticket_server_config", AsyncMock(return_value=config)),
        patch("api.tasks.arq.enqueue_job", fake_enqueue),
    )


def _job_wiring(config: TicketServerConfig):
    run_row = types.SimpleNamespace(
        workflow_id=7,
        definition=types.SimpleNamespace(workflow_configurations={}),
    )
    fake_llm = types.SimpleNamespace(
        model_name="canned",
        run_inference=AsyncMock(return_value=CANNED_SUMMARY_JSON),
    )
    return (
        patch.object(job_module.db_client, "get_workflow_run", AsyncMock(return_value=run_row)),
        patch.object(
            job_module.db_client,
            "get_workflow_by_id",
            AsyncMock(return_value=types.SimpleNamespace(user_id=3)),
        ),
        patch.object(job_module, "resolve_ticket_server_config", AsyncMock(return_value=config)),
        patch(
            "api.services.pipecat.transfer_context_handoff._verify_credential_org",
            AsyncMock(return_value=True),
        ),
        patch(
            "api.services.configuration.ai_model_configuration."
            "get_effective_ai_model_configuration_for_workflow",
            AsyncMock(return_value=types.SimpleNamespace()),
        ),
        patch(
            "api.services.pipecat.service_factory.create_llm_service",
            lambda cfg: fake_llm,
        ),
    )


async def _drain_background():
    while handoff._background_tasks:
        await asyncio.gather(*list(handoff._background_tasks), return_exceptions=True)


async def test_full_pipeline_transfer_to_screen_pop(contract_server_url):
    config = TicketServerConfig(url=contract_server_url, api_key="e2e-token")
    cap: dict = {}
    enqueued: list = []
    w1, w2, w3, w4 = _wire(config, enqueued)

    # 1. The transfer trigger (business hours → REFER).
    with w1, w2, w3, w4:
        result = await execute_cold_transfer(
            _engine(),
            room_name="cs-+886277001234",
            destination="tel:+886900000000",
            schedule=None,
            lk=_fake_lk(cap),
            transfer_reason="voice_tool",
        )
        await _drain_background()

    assert result["status"] == "success"

    # 2. REFER carried the correlation key.
    ticket_id = cap["headers"][handoff.TICKET_HEADER]
    assert ticket_id == contract.ticket_id_for_run(RUN_ID)
    assert ticket_id in cap["headers"][handoff.UUI_HEADER]

    # 3. Skeleton is immediately queryable — by ticket id and by number.
    skeleton = await handoff.call_ticket_tool(config, "get_ticket", {"ticket_id": ticket_id})
    assert skeleton["workflow_run_id"] == RUN_ID
    assert skeleton["caller_number"] == CALLER
    assert skeleton["summary"] is None  # summary not in yet

    # 4. The summary job (enqueued with the snapshot) completes it.
    assert len(enqueued) == 1
    function_name, (snapshot,) = enqueued[0]
    assert snapshot["refer_status"] == "success"
    j1, j2, j3, j4, j5, j6 = _job_wiring(config)
    with j1, j2, j3, j4, j5, j6:
        await summarize_transfer_handoff(None, snapshot)

    # 5. Screen-pop: human picks up, queries by the header's ticket id…
    ticket = await handoff.call_ticket_tool(config, "get_ticket", {"ticket_id": ticket_id})
    assert ticket["summary"]["intent"] == "dispute a double charge"
    assert ticket["summary"]["verified_identity"] == "unknown"  # never LLM-inferred
    assert ticket["notes"][0]["note_type"] == "summary"

    # 6. …or falls back to the caller number.
    found = await handoff.call_ticket_tool(
        config, "find_tickets_by_caller", {"caller_number": CALLER}
    )
    assert [t["ticket_id"] for t in found["tickets"]] == [ticket_id]


async def test_failed_refer_leaves_marked_ticket_not_ghost(contract_server_url):
    config = TicketServerConfig(url=contract_server_url, api_key="e2e-token")
    enqueued: list = []
    run_id = RUN_ID + 1
    w1, w2, w3, w4 = _wire(config, enqueued)

    with w1, w2, w3, w4:
        result = await execute_cold_transfer(
            _engine(run_id=run_id),
            room_name="cs-+886277001234",
            destination="tel:+886900000000",
            schedule=None,
            lk=_fake_lk({}, refer_fails=True),
            transfer_reason="press0",
        )
        await _drain_background()

    assert result["status"] == "failed"  # caller stays with the AI (C4)

    function_name, (snapshot,) = enqueued[0]
    assert snapshot["refer_status"] == "failed"
    j1, j2, j3, j4, j5, j6 = _job_wiring(config)
    with j1, j2, j3, j4, j5, j6:
        await summarize_transfer_handoff(None, snapshot)

    ticket_id = contract.ticket_id_for_run(run_id)
    ticket = await handoff.call_ticket_tool(config, "get_ticket", {"ticket_id": ticket_id})
    assert ticket["summary"] is None  # no fake "transferred" summary
    assert [n["note_type"] for n in ticket["notes"]] == ["transfer_failed"]
