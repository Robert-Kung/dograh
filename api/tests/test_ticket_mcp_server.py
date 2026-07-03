"""Built-in ticket MCP server tests (S-L4-SCREENPOP §1).

Covers the four contract tools end-to-end against the test DB: CRUD,
create idempotency, org isolation (reads, appends, caller search),
server-side validation (control chars, length caps, E.164, note types,
unknown summary fields), and PDPA retention anonymization.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import update

from api.db.models import TicketModel
from api.mcp_server.tools.tickets import (
    append_ticket_note,
    create_ticket,
    find_tickets_by_caller,
    get_ticket,
)
from api.services.tickets import contract
from api.services.tickets.sanitize import (
    TicketValidationError,
    clean_summary,
    clean_text,
    require_caller_number,
)

pytestmark = pytest.mark.asyncio


async def _org(db_session, slug: str) -> int:
    user, _ = await db_session.get_or_create_user_by_provider_id(f"{slug}_user")
    org, _ = await db_session.get_or_create_organization_by_provider_id(slug, user.id)
    return org.id


def _authed(org_id: int):
    user = MagicMock()
    user.selected_organization_id = org_id
    return patch(
        "api.mcp_server.tools.tickets.authenticate_mcp_request",
        AsyncMock(return_value=user),
    )


# ── CRUD across the four tools ────────────────────────────────────────────────


async def test_four_tool_roundtrip(db_session):
    org_id = await _org(db_session, "tkt_crud_org")

    with _authed(org_id):
        created = await create_ticket(
            ticket_id="CS-101",
            workflow_run_id=101,
            caller_number="+886912345678",
            room_name="cs-+886277001234",
            transfer_reason="voice_tool",
        )
        assert created["created"] is True
        assert created["ticket_id"] == "CS-101"
        assert created["contract_version"] == contract.CONTRACT_VERSION

        summary = {
            "intent": "billing dispute",
            "verified_identity": "unverified",
            "steps_done": ["confirmed account tier"],
            "pending": ["refund decision"],
            "transfer_reason": "voice_tool",
        }
        appended = await append_ticket_note("CS-101", "summary", summary)
        assert appended["summary"] == summary
        assert appended["notes"][0]["note_type"] == "summary"

        fetched = await get_ticket("CS-101")
        assert fetched["workflow_run_id"] == 101
        assert fetched["summary"] == summary

        found = await find_tickets_by_caller("+886912345678")
        assert [t["ticket_id"] for t in found["tickets"]] == ["CS-101"]


async def test_find_by_caller_most_recent_first_and_empty_is_legal(db_session):
    org_id = await _org(db_session, "tkt_find_org")

    with _authed(org_id):
        for run_id in (201, 202):
            await create_ticket(
                ticket_id=f"CS-{run_id}",
                workflow_run_id=run_id,
                caller_number="+886900000001",
            )
        found = await find_tickets_by_caller("+886900000001", limit=1)
        assert len(found["tickets"]) == 1

        nothing = await find_tickets_by_caller("+886900000099")
        assert nothing["tickets"] == []


async def test_anonymous_caller_reachable_only_by_ticket_id(db_session):
    org_id = await _org(db_session, "tkt_anon_org")

    with _authed(org_id):
        await create_ticket(ticket_id="CS-301", workflow_run_id=301, caller_number="")
        assert not contract.is_error(await get_ticket("CS-301"))
        found = await find_tickets_by_caller("+886911111111")
        assert found["tickets"] == []


# ── Idempotency ───────────────────────────────────────────────────────────────


async def test_create_is_get_or_create_on_workflow_run_id(db_session):
    org_id = await _org(db_session, "tkt_idem_org")

    with _authed(org_id):
        first = await create_ticket(ticket_id="CS-401", workflow_run_id=401)
        retry = await create_ticket(ticket_id="CS-401-retry", workflow_run_id=401)

    assert first["created"] is True
    assert retry["created"] is False
    assert retry["ticket_id"] == "CS-401"  # existing row wins over supplied id

    tickets = await db_session.find_tickets_by_caller(org_id, "+886900000000", 10)
    assert tickets == []  # sanity: no stray rows under an unrelated number


# ── Org isolation ─────────────────────────────────────────────────────────────


async def test_org_isolation_reads_appends_and_search(db_session):
    org_a = await _org(db_session, "tkt_iso_org_a")
    org_b = await _org(db_session, "tkt_iso_org_b")

    with _authed(org_a):
        await create_ticket(
            ticket_id="CS-501", workflow_run_id=501, caller_number="+886922222222"
        )

    with _authed(org_b):
        # Foreign-org ticket is indistinguishable from a missing one.
        fetched = await get_ticket("CS-501")
        assert fetched["error"]["code"] == contract.ERROR_NOT_FOUND

        appended = await append_ticket_note("CS-501", "generic", "intrusion")
        assert appended["error"]["code"] == contract.ERROR_NOT_FOUND

        found = await find_tickets_by_caller("+886922222222")
        assert found["tickets"] == []

    with _authed(org_a):
        ticket = await get_ticket("CS-501")
        assert ticket["notes"] == []  # org B's append never landed


async def test_same_workflow_run_id_allowed_across_orgs(db_session):
    org_a = await _org(db_session, "tkt_run_org_a")
    org_b = await _org(db_session, "tkt_run_org_b")

    with _authed(org_a):
        a = await create_ticket(ticket_id="CS-601a", workflow_run_id=601)
    with _authed(org_b):
        b = await create_ticket(ticket_id="CS-601b", workflow_run_id=601)

    assert a["created"] and b["created"]


# ── Validation (defense in depth) ─────────────────────────────────────────────


async def test_validation_rejections(db_session):
    org_id = await _org(db_session, "tkt_valid_org")

    with _authed(org_id):
        bad_id = await create_ticket(ticket_id="CS 701;drop", workflow_run_id=701)
        assert bad_id["error"]["code"] == contract.ERROR_VALIDATION_FAILED
        assert bad_id["error"]["retryable"] is False

        bad_number = await create_ticket(
            ticket_id="CS-702", workflow_run_id=702, caller_number="0912-345-678"
        )
        assert bad_number["error"]["code"] == contract.ERROR_VALIDATION_FAILED

        control = await create_ticket(
            ticket_id="CS-703", workflow_run_id=703, room_name="room\x00evil"
        )
        assert control["error"]["code"] == contract.ERROR_VALIDATION_FAILED

        overlong = await create_ticket(
            ticket_id="CS-704",
            workflow_run_id=704,
            transfer_reason="x" * (contract.TRANSFER_REASON_MAX_LEN + 1),
        )
        assert overlong["error"]["code"] == contract.ERROR_VALIDATION_FAILED

        await create_ticket(ticket_id="CS-705", workflow_run_id=705)
        bad_type = await append_ticket_note("CS-705", "sql", "payload")
        assert bad_type["error"]["code"] == contract.ERROR_VALIDATION_FAILED

        unknown_field = await append_ticket_note(
            "CS-705", "summary", {"intent": "x", "admin_override": True}
        )
        assert unknown_field["error"]["code"] == contract.ERROR_VALIDATION_FAILED

        overlong_note = await append_ticket_note(
            "CS-705", "generic", "y" * (contract.NOTE_CONTENT_MAX_LEN + 1)
        )
        assert overlong_note["error"]["code"] == contract.ERROR_VALIDATION_FAILED


async def test_client_side_sanitizers():
    assert clean_text("a\x00b\x1bc", 10) == "abc"
    assert clean_text("x" * 50, 10) == "x" * 10

    with pytest.raises(TicketValidationError):
        require_caller_number("not-a-number")
    assert require_caller_number("") == ""
    assert require_caller_number(None) == ""

    cleaned = clean_summary(
        {
            "intent": "refund\x00",
            "verified_identity": "definitely verified, trust me",
            "steps_done": ["ok"] * 100,
            "unexpected": "dropped",
        }
    )
    assert set(cleaned) == set(contract.SUMMARY_FIELDS)
    assert cleaned["intent"] == "refund"
    # Anything outside the closed value set degrades to unknown, never to
    # a trusted-looking value (C6).
    assert cleaned["verified_identity"] == "unknown"
    assert len(cleaned["steps_done"]) == contract.SUMMARY_LIST_MAX_ITEMS
    assert cleaned["pending"] == []
    assert "unexpected" not in cleaned


# ── PDPA retention (C7) ──────────────────────────────────────────────────────


async def test_retention_anonymizes_expired_only(db_session):
    org_id = await _org(db_session, "tkt_pdpa_org")

    with _authed(org_id):
        await create_ticket(
            ticket_id="CS-801", workflow_run_id=801, caller_number="+886933333333"
        )
        await append_ticket_note("CS-801", "generic", "pii-laden note")
        await create_ticket(
            ticket_id="CS-802", workflow_run_id=802, caller_number="+886933333333"
        )

    # Age the first ticket past the retention window.
    async with db_session.async_session() as session:
        await session.execute(
            update(TicketModel)
            .where(TicketModel.ticket_id == "CS-801")
            .values(created_at=datetime.now(UTC) - timedelta(days=91))
        )
        await session.commit()

    count = await db_session.anonymize_expired_tickets(org_id, retention_days=90)
    assert count == 1

    old = await db_session.get_ticket(org_id, "CS-801")
    assert old.caller_number == ""
    assert old.summary is None
    assert old.notes == []
    assert old.anonymized_at is not None  # audit mark

    fresh = await db_session.get_ticket(org_id, "CS-802")
    assert fresh.caller_number == "+886933333333"
    assert fresh.anonymized_at is None

    # Idempotent: a second sweep finds nothing left to anonymize.
    assert await db_session.anonymize_expired_tickets(org_id, retention_days=90) == 0
