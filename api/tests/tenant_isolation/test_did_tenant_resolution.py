"""DID globally unique + resolution returns the tenant (mt-s1-foundation 3.x).

tenant-isolation〈來電號碼解析租戶〉.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from api.db.models import CcpTenantModel, TelephonyPhoneNumberModel
from api.db.telephony_phone_number_client import ResolvedDid
from api.routes import livekit as livekit_route
from api.services.pipecat import livekit_dispatcher
from api.services.pipecat.livekit_dispatcher import SIP_DIALED_ATTRIBUTE

URL = "/api/v1/livekit/did-mappings"
DID = "+886212345678"


async def _tenant(db_session, async_session, tag: str, *, status="active"):
    user, _ = await db_session.get_or_create_user_by_provider_id(f"did_t_{tag}")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"did_t_org_{tag}", user.id
    )
    await db_session.add_user_to_organization(user.id, org.id)
    await db_session.update_user_selected_organization(user.id, org.id)
    async_session.add(
        CcpTenantModel(organization_id=org.id, display_name=tag, status=status)
    )
    await async_session.flush()
    user = await db_session.get_user_by_id(user.id)
    wf = await db_session.create_workflow(
        name=f"AI {tag}",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org.id,
    )
    return user, org, wf


async def _map(test_client_factory, user, address, workflow_id):
    async with test_client_factory(user) as client:
        return await client.post(
            URL, json={"address": address, "workflow_id": workflow_id}
        )


async def _rows(async_session, canonical=DID) -> int:
    return (
        await async_session.execute(
            select(func.count())
            .select_from(TelephonyPhoneNumberModel)
            .where(TelephonyPhoneNumberModel.address_normalized == canonical)
        )
    ).scalar_one()


@pytest.fixture(autouse=True)
def no_provider_sync():
    with patch(
        "api.routes.organization._sync_inbound_for_phone_number",
        new_callable=AsyncMock,
    ):
        yield


# --- 3.1 red: second org grabbing a DID used to DoS the first -----------------


@pytest.mark.scenario("tenant-isolation:第二租戶搶登已用號碼")
async def test_second_tenant_cannot_take_registered_did(
    test_client_factory, db_session, async_session
):
    ua, orga, wfa = await _tenant(db_session, async_session, "a")
    ub, orgb, wfb = await _tenant(db_session, async_session, "b")
    assert (await _map(test_client_factory, ua, DID, wfa.id)).status_code == 201

    resp = await _map(test_client_factory, ub, "02-1234-5678", wfb.id)

    assert resp.status_code == 409
    assert resp.json() == {"detail": "did_taken"}
    assert str(orga.id) not in resp.text and str(wfa.id) not in resp.text
    assert await _rows(async_session) == 1
    resolved = await db_session.find_inbound_workflow_for_did(DID)
    assert resolved is not None
    assert (resolved.workflow_id, resolved.organization_id) == (wfa.id, orga.id)
    assert resolved.mapped is True


# --- 3.9 red: variant spellings must write and resolve to one string ----------


@pytest.mark.scenario("tenant-isolation:號碼變體格式解析一致")
@pytest.mark.parametrize(
    "dialed",
    [
        "886212345678",
        "+886 2-1234-5678",
        "02-1234-5678",
        "tel:+886212345678",
        "sip:+886212345678@sip.example.net",
    ],
)
async def test_variant_spellings_resolve_to_registered_row(
    test_client_factory, db_session, async_session, dialed
):
    user, org, wf = await _tenant(db_session, async_session, "v")
    assert (
        await _map(test_client_factory, user, "+886 2-1234-5678", wf.id)
    ).status_code == 201
    resolved = await livekit_route._did_resolver(dialed)
    assert resolved is not None and resolved.workflow_id == wf.id


@pytest.mark.scenario("tenant-isolation:號碼變體格式解析一致")
@pytest.mark.parametrize(
    "variant", ["886212345678", "tel:+886212345678", "sip:+886212345678@host"]
)
async def test_variant_spelling_reregistration_is_did_taken(
    test_client_factory, db_session, async_session, variant
):
    ua, _, wfa = await _tenant(db_session, async_session, "va")
    ub, _, wfb = await _tenant(db_session, async_session, "vb")
    assert (
        await _map(test_client_factory, ua, "+886 2-1234-5678", wfa.id)
    ).status_code == 201
    resp = await _map(test_client_factory, ub, variant, wfb.id)
    assert resp.status_code == 409 and resp.json() == {"detail": "did_taken"}
    assert await _rows(async_session) == 1


# --- 3.4 telephony number API and migration pre-check -------------------------


@pytest.mark.scenario("tenant-isolation:第二租戶搶登已用號碼")
async def test_telephony_number_api_refuses_number_held_elsewhere(
    test_client_factory, db_session, async_session
):
    ua, orga, wfa = await _tenant(db_session, async_session, "ta")
    ub, orgb, _ = await _tenant(db_session, async_session, "tb")
    assert (await _map(test_client_factory, ua, DID, wfa.id)).status_code == 201
    cfg = await db_session.create_telephony_configuration(
        organization_id=orgb.id, name="twilio-b", provider="twilio", credentials={}
    )
    async with test_client_factory(ub) as client:
        resp = await client.post(
            f"/api/v1/organizations/telephony-configs/{cfg.id}/phone-numbers",
            json={"address": "+886 2-1234-5678", "country_code": "TW"},
        )
    assert resp.status_code == 409 and resp.json() == {"detail": "did_taken"}
    assert str(orga.id) not in resp.text
    assert await _rows(async_session) == 1


async def test_same_org_duplicate_is_not_reported_as_held_elsewhere(
    test_client_factory, db_session, async_session
):
    """review gate R10: the org's own number on another config is not
    did_taken (which says another tenant holds it)."""
    ua, orga, wfa = await _tenant(db_session, async_session, "sa")
    assert (await _map(test_client_factory, ua, DID, wfa.id)).status_code == 201
    cfg = await db_session.create_telephony_configuration(
        organization_id=orga.id, name="twilio-a", provider="twilio", credentials={}
    )
    async with test_client_factory(ua) as client:
        resp = await client.post(
            f"/api/v1/organizations/telephony-configs/{cfg.id}/phone-numbers",
            json={"address": "+886 2-1234-5678", "country_code": "TW"},
        )
    # No row count here: the test session's savepoint rollback after the
    # IntegrityError also drops the first row (harness artifact).
    assert resp.status_code == 409
    assert resp.json() != {"detail": "did_taken"}


async def test_same_org_duplicate_raises_integrity_error_not_did_taken(
    db_session, async_session
):
    """review gate R10: hitting the unique index on the own org's number
    re-raises the IntegrityError (organization routes answer 409 "already
    exists in the org"; livekit did-mapping a retryable 503)."""
    _, orga, _ = await _tenant(db_session, async_session, "ra")
    cfg1 = await db_session.create_telephony_configuration(
        organization_id=orga.id, name="cfg1", provider="livekit", credentials={}
    )
    cfg2 = await db_session.create_telephony_configuration(
        organization_id=orga.id, name="cfg2", provider="twilio", credentials={}
    )
    await db_session.create_phone_number(
        organization_id=orga.id, telephony_configuration_id=cfg1.id, address=DID
    )
    with pytest.raises(IntegrityError):
        await db_session.create_phone_number(
            organization_id=orga.id, telephony_configuration_id=cfg2.id, address=DID
        )


async def test_legacy_save_reports_skipped_numbers(
    test_client_factory, db_session, async_session
):
    """review gate R9: the legacy telephony-config save tells the caller which
    numbers were not registered and why, without describing the holder."""
    ua, orga, wfa = await _tenant(db_session, async_session, "la")
    ub, orgb, _ = await _tenant(db_session, async_session, "lb")
    assert (await _map(test_client_factory, ua, DID, wfa.id)).status_code == 201
    body = {
        "provider": "twilio",
        "account_sid": "AC-legacy-b",
        "auth_token": "tok-b",
        "from_numbers": [DID, "+886221117777", "   "],
    }
    async with test_client_factory(ub) as client:
        resp = await client.post("/api/v1/organizations/telephony-config", json=body)

    assert resp.status_code == 200
    assert resp.json()["skipped_numbers"] == [
        {"index": 0, "address_masked": "***5678", "reason": "did_taken"},
        {"index": 2, "address_masked": "***   ", "reason": "invalid"},
    ]
    assert DID not in resp.text and str(orga.id) not in resp.text


def _phone_migration():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/c5e2a8d4f1b6_phone_numbers_global_unique.py"
    )
    spec = importlib.util.spec_from_file_location("phone_mig", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.scenario("tenant-isolation:既有重複號碼使 migration 中止")
async def test_migration_precheck_aborts_and_masks_duplicates(
    db_session, async_session
):
    _, orga, _ = await _tenant(db_session, async_session, "ma")
    _, orgb, _ = await _tenant(db_session, async_session, "mb")
    conn = await async_session.connection()
    # Recreate the pre-migration world inside the test transaction.
    await async_session.execute(text("DROP INDEX uq_phone_numbers_address_global"))
    for org in (orga, orgb):
        cfg = await db_session.create_telephony_configuration(
            organization_id=org.id, name=f"c{org.id}", provider="twilio", credentials={}
        )
        await async_session.execute(
            text(
                "INSERT INTO telephony_phone_numbers (organization_id, "
                "telephony_configuration_id, address, address_normalized, "
                "address_type, is_active, is_default_caller_id, extra_metadata) "
                "VALUES (:o, :c, :a, :a, 'pstn', true, false, '{}')"
            ),
            {"o": org.id, "c": cfg.id, "a": DID},
        )
    mod = _phone_migration()
    with pytest.raises(mod.DuplicatePhoneNumbers) as exc:
        await conn.run_sync(mod.check_no_duplicates)
    msg = str(exc.value)
    assert "***5678" in msg and DID not in msg
    assert str(orga.id) in msg and str(orgb.id) in msg
    assert await _rows(async_session) == 2


# --- 3.5–3.7 resolution semantics ----------------------------------------------


async def _number(db_session, org, wf_id, *, active=True):
    cfg = await db_session.create_telephony_configuration(
        organization_id=org.id, name=f"n{org.id}", provider="livekit", credentials={}
    )
    return await db_session.create_phone_number(
        organization_id=org.id,
        telephony_configuration_id=cfg.id,
        address=DID,
        inbound_workflow_id=wf_id,
        is_active=active,
    )


@pytest.mark.scenario("tenant-isolation:號碼與工作流分屬不同組織")
@pytest.mark.scenario("livekit-dispatch:組織不一致視同未設定")
async def test_number_and_workflow_in_different_orgs_is_unmapped_but_owned(
    db_session, async_session
):
    _, orga, _ = await _tenant(db_session, async_session, "xa")
    _, _, wfb = await _tenant(db_session, async_session, "xb")
    await _number(db_session, orga, wfb.id)
    resolved = await livekit_route._did_resolver(DID)
    assert resolved == ResolvedDid(None, orga.id, "active", False)


async def test_unmapped_first_tenant_number_uses_platform_fallback(monkeypatch):
    monkeypatch.setenv("CCP_FIRST_TENANT_ORGANIZATION_ID", "7")
    fallback = AsyncMock()

    async def resolver(did):
        return ResolvedDid(None, 7, "active", False)

    handled = await livekit_dispatcher._dispatch(
        "cs-room", {SIP_DIALED_ATTRIBUTE: DID}, resolver, fallback, "ws://x"
    )
    assert handled is True
    fallback.assert_awaited_once_with("cs-room", "unmapped_did", None)


@pytest.mark.scenario("tenant-isolation:停用號碼列仍歸屬其組織")
async def test_inactive_number_still_owned(db_session, async_session):
    _, org, wf = await _tenant(db_session, async_session, "in")
    await _number(db_session, org, wf.id, active=False)
    assert await livekit_route._did_resolver(DID) == ResolvedDid(
        None, org.id, "active", False
    )


@pytest.mark.scenario("tenant-isolation:非租戶組織")
@pytest.mark.scenario("livekit-dispatch:組織不一致視同未設定")
async def test_non_tenant_organization_is_unmapped(db_session, async_session):
    user, _ = await db_session.get_or_create_user_by_provider_id("did_nt")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        "did_nt_org", user.id
    )
    wf = await db_session.create_workflow(
        name="nt",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org.id,
    )
    await _number(db_session, org, wf.id)
    assert await livekit_route._did_resolver(DID) == ResolvedDid(
        None, org.id, None, False
    )


async def test_unknown_number_resolves_to_none(db_session):
    assert await livekit_route._did_resolver("+886299999999") is None
    assert await livekit_route._did_resolver("not-a-number") is None


@pytest.mark.scenario("tenant-isolation:解析回傳號碼列的組織")
async def test_creator_switching_org_does_not_change_resolution(
    db_session, async_session
):
    user, orga, wf = await _tenant(db_session, async_session, "sw")
    _, orgb, _ = await _tenant(db_session, async_session, "sw2")
    await _number(db_session, orga, wf.id)
    await db_session.update_user_selected_organization(user.id, orgb.id)
    assert await livekit_route._did_resolver(DID) == ResolvedDid(
        wf.id, orga.id, "active", True
    )


@pytest.mark.scenario("tenant-isolation:停用租戶的號碼仍解析出組織")
async def test_suspended_tenant_still_resolves_with_status(db_session, async_session):
    _, org, wf = await _tenant(db_session, async_session, "su", status="suspended")
    await _number(db_session, org, wf.id)
    assert await livekit_route._did_resolver(DID) == ResolvedDid(
        wf.id, org.id, "suspended", True
    )


# --- review gate S2/R4 (user ruling 2026-11-25): end, never REFER elsewhere --


async def _dispatch_with(monkeypatch, resolved):
    from api.db import db_client

    async def no_run(*a, **k):
        raise AssertionError("no run may be created")

    monkeypatch.setattr(db_client, "create_workflow_run", no_run)
    fallback = AsyncMock()

    async def resolver(did):
        return resolved

    handled = await livekit_dispatcher._dispatch(
        "cs-room", {SIP_DIALED_ATTRIBUTE: DID}, resolver, fallback, "ws://x"
    )
    assert handled is True
    return fallback


@pytest.mark.scenario("tenant-isolation:號碼與工作流分屬不同組織")
@pytest.mark.scenario("tenant-isolation:停用號碼列仍歸屬其組織")
@pytest.mark.scenario("livekit-dispatch:組織不一致視同未設定")
async def test_unmapped_other_org_number_ends_without_refer(monkeypatch):
    monkeypatch.setenv("CCP_FIRST_TENANT_ORGANIZATION_ID", "1")
    fallback = await _dispatch_with(monkeypatch, ResolvedDid(None, 7, "active", False))
    fallback.assert_awaited_once_with(
        "cs-room", "unmapped_did_other_org", None, end_only=True
    )


async def test_non_tenant_org_number_ends_without_refer(monkeypatch):
    monkeypatch.setenv("CCP_FIRST_TENANT_ORGANIZATION_ID", "1")
    fallback = await _dispatch_with(monkeypatch, ResolvedDid(None, 7, None, False))
    fallback.assert_awaited_once_with(
        "cs-room", "unmapped_did_other_org", None, end_only=True
    )


async def test_unset_first_tenant_ends_rather_than_refer(monkeypatch):
    monkeypatch.delenv("CCP_FIRST_TENANT_ORGANIZATION_ID", raising=False)
    fallback = await _dispatch_with(monkeypatch, ResolvedDid(None, 1, "active", False))
    fallback.assert_awaited_once_with(
        "cs-room", "unmapped_did_other_org", None, end_only=True
    )


@pytest.mark.scenario("livekit-dispatch:停用租戶來電")
@pytest.mark.scenario("tenant-isolation:停用租戶的號碼仍解析出組織")
@pytest.mark.parametrize("first", ["1", "7"], ids=["other-org", "first-tenant"])
async def test_suspended_tenant_call_ends_before_admission(monkeypatch, first):
    from api.services.pipecat import active_calls

    monkeypatch.setenv("CCP_FIRST_TENANT_ORGANIZATION_ID", first)
    monkeypatch.setattr(
        active_calls,
        "try_acquire_slot",
        lambda *a: (_ for _ in ()).throw(AssertionError("no slot")),
    )
    fallback = await _dispatch_with(monkeypatch, ResolvedDid(11, 7, "suspended", True))
    fallback.assert_awaited_once_with(
        "cs-room", "tenant_suspended", None, end_only=True
    )
