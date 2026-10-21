"""``POST /api/v1/livekit/did-mappings`` — the deployment-bootstrap path that
binds a DID to an AI workflow for LiveKit inbound (real test DB, full app).

The mapping must land where the dispatcher looks
(``find_inbound_workflow_for_did``), under a ``provider='livekit'`` FK
container that never becomes the org's default outbound, without any
provider inbound sync, and without touching other orgs or other providers'
numbers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.services.pipecat.livekit_dispatcher import (
    SIP_DIALED_ATTRIBUTE,
    did_from_sip_attributes,
    normalize_dialed_did,
)

URL = "/api/v1/livekit/did-mappings"
DID_LOCAL = "02-1234-5678"
DID = "+886212345678"
SECRET = "sk-should-never-leak-9f3a"


async def _user_with_org(db_session, tag: str):
    user, _ = await db_session.get_or_create_user_by_provider_id(f"lk_did_{tag}")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"lk_did_org_{tag}", user.id
    )
    await db_session.update_user_selected_organization(user.id, org.id)
    return await db_session.get_user_by_id(user.id), org


async def _workflow(db_session, user, org, name="AI"):
    return await db_session.create_workflow(
        name=name,
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org.id,
    )


async def _post(test_client_factory, user, **body):
    async with test_client_factory(user) as client:
        return await client.post(URL, json=body)


async def _livekit_configs(db_session, org):
    return await db_session.list_telephony_configurations_by_provider(org.id, "livekit")


@pytest.fixture
def no_provider_sync():
    """Any provider inbound sync on this path is a bug: no transport exists."""
    with (
        patch(
            "api.routes.organization._sync_inbound_for_phone_number",
            new_callable=AsyncMock,
        ) as sync,
        patch(
            "api.services.telephony.factory.get_telephony_provider_by_id",
            new_callable=AsyncMock,
        ) as factory,
    ):
        yield sync, factory
    sync.assert_not_awaited()
    factory.assert_not_awaited()


# ── created ─────────────────────────────────────────────────────────────────


async def test_fresh_org_creates_container_and_number_routable_by_dispatcher(
    test_client_factory, db_session, no_provider_sync
):
    user, org = await _user_with_org(db_session, "fresh")
    wf = await _workflow(db_session, user, org)

    response = await _post(
        test_client_factory, user, address=DID_LOCAL, workflow_id=wf.id
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "created"
    assert body["address_normalized"] == DID
    assert body["workflow_id"] == wf.id
    assert body["current"] is None

    [cfg] = await _livekit_configs(db_session, org)
    assert body["config_id"] == cfg.id
    assert cfg.name == "livekit"
    assert cfg.credentials == {}
    # The org's first config, yet never its default outbound.
    assert cfg.is_default_outbound is False
    assert await db_session.get_default_telephony_configuration(org.id) is None

    row = await db_session.get_phone_number(body["phone_number_id"])
    assert row.telephony_configuration_id == cfg.id
    assert row.address_normalized == DID
    assert row.address_type == "pstn"
    assert row.label == "ai-inbound"
    assert row.country_code == "TW"
    assert row.is_active is True
    assert row.inbound_workflow_id == wf.id

    # The dialed number as the SIP trunk delivers it resolves to this workflow.
    dialed = did_from_sip_attributes({SIP_DIALED_ATTRIBUTE: "0212345678"})
    assert await db_session.find_inbound_workflow_for_did(dialed) == (wf.id, user.id)


async def test_existing_livekit_config_is_reused(
    test_client_factory, db_session, no_provider_sync
):
    user, org = await _user_with_org(db_session, "reuse")
    wf = await _workflow(db_session, user, org)
    legacy = await db_session.create_telephony_configuration(
        organization_id=org.id,
        name="acceptance-livekit",
        provider="livekit",
        credentials={},
        never_default_outbound=True,
    )

    response = await _post(test_client_factory, user, address=DID, workflow_id=wf.id)

    assert response.status_code == 201, response.text
    assert response.json()["config_id"] == legacy.id
    assert [c.id for c in await _livekit_configs(db_session, org)] == [legacy.id]


async def test_existing_default_outbound_is_kept(
    test_client_factory, db_session, no_provider_sync
):
    user, org = await _user_with_org(db_session, "keepdefault")
    wf = await _workflow(db_session, user, org)
    twilio = await db_session.create_telephony_configuration(
        organization_id=org.id,
        name="twilio-prod",
        provider="twilio",
        credentials={"account_sid": "AC1", "auth_token": SECRET},
    )
    assert twilio.is_default_outbound is True

    response = await _post(test_client_factory, user, address=DID, workflow_id=wf.id)

    assert response.status_code == 201, response.text
    [cfg] = await _livekit_configs(db_session, org)
    assert cfg.is_default_outbound is False
    default = await db_session.get_default_telephony_configuration(org.id)
    assert default.id == twilio.id


async def test_first_config_without_flag_still_becomes_default(db_session):
    """``never_default_outbound`` must not change the existing default rule."""
    user, org = await _user_with_org(db_session, "flagoff")
    cfg = await db_session.create_telephony_configuration(
        organization_id=org.id,
        name="first",
        provider="twilio",
        credentials={"account_sid": "AC2"},
    )
    assert cfg.is_default_outbound is True


async def test_foreign_e164_keeps_its_country(
    test_client_factory, db_session, no_provider_sync
):
    user, org = await _user_with_org(db_session, "foreign")
    wf = await _workflow(db_session, user, org)

    response = await _post(
        test_client_factory, user, address="+1 555 123 4567", workflow_id=wf.id
    )

    assert response.status_code == 201, response.text
    assert response.json()["address_normalized"] == "+15551234567"
    row = await db_session.get_phone_number(response.json()["phone_number_id"])
    assert row.address_normalized == "+15551234567"
    assert row.country_code is None


# ── unchanged ───────────────────────────────────────────────────────────────


async def test_repeat_call_is_unchanged_with_zero_writes(
    test_client_factory, db_session, no_provider_sync
):
    user, org = await _user_with_org(db_session, "unchanged")
    wf = await _workflow(db_session, user, org)
    first = await _post(test_client_factory, user, address=DID, workflow_id=wf.id)
    assert first.status_code == 201

    writes = [
        "create_telephony_configuration",
        "create_phone_number",
        "update_phone_number",
        "update_telephony_configuration",
        "set_default_telephony_configuration",
    ]
    mocks = {name: AsyncMock(name=name) for name in writes}
    with patch.multiple(db_session, **mocks):
        again = await _post(
            test_client_factory, user, address=DID_LOCAL, workflow_id=wf.id
        )

    assert again.status_code == 200, again.text
    body = again.json()
    assert body["status"] == "unchanged"
    for key in ("config_id", "phone_number_id", "address_normalized", "workflow_id"):
        assert body[key] == first.json()[key]
    for name, mock in mocks.items():
        assert not mock.called, name


# ── conflict / rebound ──────────────────────────────────────────────────────


async def _seed_livekit_row(
    db_session, org, workflow_id, *, is_active=True, address=DID
):
    cfg = await db_session.create_telephony_configuration(
        organization_id=org.id,
        name="livekit",
        provider="livekit",
        credentials={},
        never_default_outbound=True,
    )
    row = await db_session.create_phone_number(
        organization_id=org.id,
        telephony_configuration_id=cfg.id,
        address=address,
        country_code="TW",
        inbound_workflow_id=workflow_id,
        is_active=is_active,
    )
    return cfg, row


async def test_bound_to_other_workflow_conflicts_then_overwrite_rebinds(
    test_client_factory, db_session, no_provider_sync
):
    user, org = await _user_with_org(db_session, "rebind")
    old = await _workflow(db_session, user, org, "old")
    new = await _workflow(db_session, user, org, "new")
    cfg, row = await _seed_livekit_row(db_session, org, old.id)

    conflict = await _post(test_client_factory, user, address=DID, workflow_id=new.id)

    assert conflict.status_code == 409, conflict.text
    assert conflict.json() == {
        "status": "conflict",
        "config_id": cfg.id,
        "phone_number_id": row.id,
        "address_normalized": DID,
        "workflow_id": new.id,
        "current": {"workflow_id": old.id, "is_active": True, "provider": "livekit"},
    }
    assert (await db_session.get_phone_number(row.id)).inbound_workflow_id == old.id

    rebound = await _post(
        test_client_factory, user, address=DID, workflow_id=new.id, overwrite=True
    )

    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["status"] == "rebound"
    assert rebound.json()["phone_number_id"] == row.id
    after = await db_session.get_phone_number(row.id)
    assert after.inbound_workflow_id == new.id
    assert after.is_active is True
    assert await db_session.find_inbound_workflow_for_did(DID) == (new.id, user.id)


async def test_inactive_row_conflicts_then_overwrite_activates(
    test_client_factory, db_session, no_provider_sync
):
    user, org = await _user_with_org(db_session, "inactive")
    wf = await _workflow(db_session, user, org)
    _, row = await _seed_livekit_row(db_session, org, wf.id, is_active=False)

    conflict = await _post(test_client_factory, user, address=DID, workflow_id=wf.id)

    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["current"] == {
        "workflow_id": wf.id,
        "is_active": False,
        "provider": "livekit",
    }

    rebound = await _post(
        test_client_factory, user, address=DID, workflow_id=wf.id, overwrite=True
    )

    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["status"] == "rebound"
    assert (await db_session.get_phone_number(row.id)).is_active is True


async def test_unbound_row_conflicts(test_client_factory, db_session, no_provider_sync):
    user, org = await _user_with_org(db_session, "unbound")
    wf = await _workflow(db_session, user, org)
    await _seed_livekit_row(db_session, org, None)

    response = await _post(test_client_factory, user, address=DID, workflow_id=wf.id)

    assert response.status_code == 409, response.text
    assert response.json()["current"]["workflow_id"] is None


@pytest.mark.parametrize("overwrite", [False, True])
async def test_number_of_another_provider_is_never_moved(
    test_client_factory, db_session, no_provider_sync, overwrite
):
    user, org = await _user_with_org(db_session, f"foreignprov{overwrite}")
    wf = await _workflow(db_session, user, org)
    twilio = await db_session.create_telephony_configuration(
        organization_id=org.id,
        name="twilio-prod",
        provider="twilio",
        credentials={"account_sid": "AC9", "auth_token": SECRET},
    )
    row = await db_session.create_phone_number(
        organization_id=org.id,
        telephony_configuration_id=twilio.id,
        address=DID,
        is_active=True,
    )

    response = await _post(
        test_client_factory, user, address=DID, workflow_id=wf.id, overwrite=overwrite
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["status"] == "conflict"
    assert body["current"] == {
        "workflow_id": None,
        "is_active": True,
        "provider": "twilio",
    }
    assert SECRET not in response.text
    assert "credentials" not in response.text
    after = await db_session.get_phone_number(row.id)
    assert after.telephony_configuration_id == twilio.id
    assert after.inbound_workflow_id is None
    # No container is created on a refused request.
    assert await _livekit_configs(db_session, org) == []


# ── container config ambiguity ──────────────────────────────────────────────


async def test_two_livekit_configs_conflict(
    test_client_factory, db_session, no_provider_sync
):
    user, org = await _user_with_org(db_session, "twolk")
    wf = await _workflow(db_session, user, org)
    for name in ("lk-a", "lk-b"):
        await db_session.create_telephony_configuration(
            organization_id=org.id,
            name=name,
            provider="livekit",
            credentials={},
            never_default_outbound=True,
        )

    response = await _post(test_client_factory, user, address=DID, workflow_id=wf.id)

    # 422, not 409: overwrite cannot fix it, so it must not read as "bound elsewhere".
    assert response.status_code == 422, response.text
    assert "2 livekit" in response.json()["detail"]
    assert (
        await db_session.get_phone_number_with_provider_by_address(org.id, DID) is None
    )


async def test_container_name_taken_by_other_provider_conflicts(
    test_client_factory, db_session, no_provider_sync
):
    user, org = await _user_with_org(db_session, "nametaken")
    wf = await _workflow(db_session, user, org)
    await db_session.create_telephony_configuration(
        organization_id=org.id,
        name="livekit",
        provider="twilio",
        credentials={"account_sid": "AC3", "auth_token": SECRET},
    )

    response = await _post(test_client_factory, user, address=DID, workflow_id=wf.id)

    assert response.status_code == 422, response.text
    assert SECRET not in response.text
    assert await _livekit_configs(db_session, org) == []


async def test_rebind_racing_a_delete_is_503_not_500(
    test_client_factory, db_session, no_provider_sync
):
    user, org = await _user_with_org(db_session, "race")
    old = await _workflow(db_session, user, org, "old")
    new = await _workflow(db_session, user, org, "new")
    await _seed_livekit_row(db_session, org, old.id)

    with patch(
        "api.services.pipecat.livekit_did_mapping.db_client.update_phone_number",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await _post(
            test_client_factory, user, address=DID, workflow_id=new.id, overwrite=True
        )

    assert response.status_code == 503, response.text


async def test_concurrent_create_is_503(
    test_client_factory, db_session, no_provider_sync
):
    from sqlalchemy.exc import IntegrityError

    user, org = await _user_with_org(db_session, "concurrent")
    wf = await _workflow(db_session, user, org)

    with patch(
        "api.services.pipecat.livekit_did_mapping.db_client.create_phone_number",
        new_callable=AsyncMock,
        side_effect=IntegrityError("insert", {}, Exception("dup")),
    ):
        response = await _post(
            test_client_factory, user, address=DID, workflow_id=wf.id
        )

    assert response.status_code == 503, response.text


# ── validation / scoping ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address", ["sip:ai@pbx.example.com", "1234", "agent-desk", "   "]
)
async def test_non_pstn_address_is_422(
    test_client_factory, db_session, no_provider_sync, address
):
    user, org = await _user_with_org(db_session, "nonpstn")
    wf = await _workflow(db_session, user, org)

    response = await _post(
        test_client_factory, user, address=address, workflow_id=wf.id
    )

    assert response.status_code == 422, response.text
    assert await _livekit_configs(db_session, org) == []


async def test_missing_body_field_is_422(test_client_factory, db_session):
    user, _ = await _user_with_org(db_session, "nofield")

    response = await _post(test_client_factory, user, address=DID)

    assert response.status_code == 422


async def test_workflow_of_another_org_is_404(
    test_client_factory, db_session, no_provider_sync
):
    user_a, org_a = await _user_with_org(db_session, "wfiso_a")
    user_b, org_b = await _user_with_org(db_session, "wfiso_b")
    wf_b = await _workflow(db_session, user_b, org_b)

    response = await _post(
        test_client_factory, user_a, address=DID, workflow_id=wf_b.id
    )

    assert response.status_code == 404, response.text
    assert await _livekit_configs(db_session, org_a) == []


async def test_unknown_workflow_is_404(
    test_client_factory, db_session, no_provider_sync
):
    user, _ = await _user_with_org(db_session, "nowf")

    response = await _post(
        test_client_factory, user, address=DID, workflow_id=987654321
    )

    assert response.status_code == 404, response.text


async def test_orgs_are_isolated(test_client_factory, db_session, no_provider_sync):
    user_a, org_a = await _user_with_org(db_session, "iso_a")
    user_b, org_b = await _user_with_org(db_session, "iso_b")
    wf_a = await _workflow(db_session, user_a, org_a)
    wf_b = await _workflow(db_session, user_b, org_b)
    created_a = await _post(
        test_client_factory, user_a, address=DID, workflow_id=wf_a.id
    )
    assert created_a.status_code == 201

    created_b = await _post(
        test_client_factory, user_b, address=DID, workflow_id=wf_b.id
    )

    # Org A's row is neither a conflict for B nor reused by B.
    assert created_b.status_code == 201, created_b.text
    body_a, body_b = created_a.json(), created_b.json()
    assert body_b["config_id"] != body_a["config_id"]
    assert body_b["phone_number_id"] != body_a["phone_number_id"]
    [cfg_b] = await _livekit_configs(db_session, org_b)
    assert cfg_b.id == body_b["config_id"]
    row_a = await db_session.get_phone_number(body_a["phone_number_id"])
    assert row_a.inbound_workflow_id == wf_a.id
    assert row_a.organization_id == org_a.id


async def test_no_selected_organization_is_400(test_client_factory, db_session):
    user, _ = await db_session.get_or_create_user_by_provider_id("lk_did_noorg")
    assert user.selected_organization_id is None

    response = await _post(test_client_factory, user, address=DID, workflow_id=1)

    assert response.status_code == 400


# ── normalization shared with the dispatcher ────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0212345678", DID),
        ("212345678", DID),
        ("886212345678", DID),
        ("+886 2 1234 5678", DID),
        ("+15551234567", "+15551234567"),
        ("0015551234567", "+15551234567"),
        ("sip:ai@pbx", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_dialed_did_matches_dispatcher(raw, expected):
    assert normalize_dialed_did(raw) == expected
    assert did_from_sip_attributes({SIP_DIALED_ATTRIBUTE: raw}) == expected
