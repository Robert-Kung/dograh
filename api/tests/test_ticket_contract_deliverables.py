"""Contract deliverable tests (S-L4-SCREENPOP §4).

Substitutability is proven the hard way: the bundled reference server is
started over real streamable HTTP and the *platform's own client path*
(`call_ticket_tool`) drives all four tools against it — swap the server,
zero agent change. The conformance runner must be fully green against the
reference implementation (validating both deliverables at once), the
built-in server's registered tools must not drift from the shipped
TOOL_SCHEMAS, and the startup check must classify missing tools loudly.
"""

import pytest

from api.services.pipecat.transfer_context_handoff import call_ticket_tool
from api.services.tickets import contract
from api.services.tickets.config import TicketServerConfig
from api.services.tickets.conformance import ConformanceSession, run_conformance
from api.services.tickets.reference_server import build_reference_mcp
from api.services.tickets.startup_check import probe_ticket_server
from api.tests.support.mcp_http_server import ThreadedMcpServer

pytestmark = pytest.mark.asyncio


@pytest.fixture
def reference_url():
    """The reference server on real streamable HTTP (own thread + loop)."""
    with ThreadedMcpServer(build_reference_mcp()) as server:
        yield server.url


# ── 4.4 Substitutability: platform client ↔ reference server ────────────────


async def test_platform_client_runs_all_four_tools_against_reference(reference_url):
    config = TicketServerConfig(url=reference_url, api_key="any-token")

    created = await call_ticket_tool(
        config,
        "create_ticket",
        {
            "ticket_id": "CS-9001",
            "workflow_run_id": 9001,
            "caller_number": "+886911222333",
            "room_name": "cs-room",
            "transfer_reason": "press0",
        },
    )
    assert created["created"] is True
    assert created["contract_version"] == contract.CONTRACT_VERSION

    retry = await call_ticket_tool(
        config,
        "create_ticket",
        {"ticket_id": "CS-9001-retry", "workflow_run_id": 9001},
    )
    assert retry["created"] is False and retry["ticket_id"] == "CS-9001"

    summary = {
        "intent": "billing",
        "verified_identity": "unknown",
        "steps_done": [],
        "pending": ["refund"],
        "transfer_reason": "press0",
    }
    appended = await call_ticket_tool(
        config,
        "append_ticket_note",
        {"ticket_id": "CS-9001", "note_type": "summary", "content": summary},
    )
    assert appended["summary"] == summary

    fetched = await call_ticket_tool(config, "get_ticket", {"ticket_id": "CS-9001"})
    assert fetched["workflow_run_id"] == 9001

    found = await call_ticket_tool(
        config, "find_tickets_by_caller", {"caller_number": "+886911222333"}
    )
    assert [t["ticket_id"] for t in found["tickets"]] == ["CS-9001"]

    not_found = await call_ticket_tool(config, "get_ticket", {"ticket_id": "CS-nope"})
    assert not_found["error"]["code"] == contract.ERROR_NOT_FOUND


# ── 4.2 Conformance runner is green on the reference implementation ─────────


async def test_conformance_runner_green_on_reference(reference_url):
    session = ConformanceSession(reference_url, "any-token", timeout_seconds=10.0)
    results = await run_conformance(session)
    failures = [r for r in results if not r.passed]
    assert not failures, f"conformance failures: {failures}"
    # Every behavior family is represented, not just tools/list.
    assert len(results) >= 10


# ── Schema drift: built-in server vs shipped contract ────────────────────────


async def test_builtin_tools_match_contract_schemas():
    # Tool.from_function is the same schema generation server.py's
    # registration uses, without instantiating a FastMCP registry (which
    # leaks cross-test state into later in-process servers).
    from fastmcp.tools import Tool

    from api.mcp_server.tools.tickets import (
        append_ticket_note,
        create_ticket,
        find_tickets_by_caller,
        get_ticket,
    )

    registered = {
        t.name: t
        for t in (
            Tool.from_function(fn)
            for fn in (
                create_ticket,
                append_ticket_note,
                get_ticket,
                find_tickets_by_caller,
            )
        )
    }
    assert set(registered) == set(contract.TOOL_SCHEMAS)

    for name, spec in contract.TOOL_SCHEMAS.items():
        generated = registered[name].parameters
        contract_props = set(spec["input"]["properties"])
        generated_props = set(generated.get("properties", {}))
        assert contract_props == generated_props, (
            f"{name}: contract fields {contract_props} != "
            f"implementation fields {generated_props}"
        )
        assert set(spec["input"]["required"]) == set(generated.get("required", [])), (
            f"{name}: required-field drift"
        )


async def test_required_optional_tiering_matches_contract():
    assert (
        tuple(n for n, s in contract.TOOL_SCHEMAS.items() if s["required"])
        == contract.REQUIRED_TOOLS
    )
    assert (
        tuple(n for n, s in contract.TOOL_SCHEMAS.items() if not s["required"])
        == contract.OPTIONAL_TOOLS
    )


# ── 4.3 Startup check ────────────────────────────────────────────────────────


async def test_startup_probe_full_server_reports_clean(reference_url):
    config = TicketServerConfig(url=reference_url, api_key="any")
    status = await probe_ticket_server(config, org_id=1)
    assert status == {
        "reachable": True,
        "missing_required": [],
        "missing_optional": [],
    }


async def test_startup_probe_flags_missing_tools():
    from fastmcp import FastMCP

    partial = FastMCP("partial")

    @partial.tool
    def create_ticket(ticket_id: str, workflow_run_id: int) -> dict:
        """Stub."""
        return {}

    with ThreadedMcpServer(partial) as server:
        config = TicketServerConfig(url=server.url, api_key="any")
        status = await probe_ticket_server(config, org_id=1)

    assert status["reachable"] is True
    assert status["missing_required"] == ["append_ticket_note"]
    assert set(status["missing_optional"]) == set(contract.OPTIONAL_TOOLS)


async def test_startup_probe_unreachable_is_loud_not_fatal():
    config = TicketServerConfig(url="http://127.0.0.1:1/mcp", api_key="any")
    status = await probe_ticket_server(config, org_id=1)  # must not raise
    assert status["reachable"] is False
