"""Ambient noise storage keys are confined to the workflow's organization
(review gate R1: a workflow could play another org's call recording)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.workflow.ambient_noise import (
    foreign_ambient_noise_key,
    scoped_ambient_noise_config,
)


def _wc(key):
    return {
        "ambient_noise_configuration": {
            "enabled": True,
            "storage_key": key,
            "storage_backend": "minio",
            "volume": 0.5,
        }
    }


@pytest.mark.parametrize(
    "key, foreign",
    [
        ("ambient-noise/7/12/abc_rain.wav", False),
        ("ambient-noise/8/12/abc_rain.wav", True),
        ("recordings/7.wav", True),
        ("recordings/7/user.wav", True),
        ("ambient-noise/7/../8/x.wav", True),
        ("ambient-noise/7//x.wav", True),
    ],
)
def test_foreign_key(key, foreign):
    assert (foreign_ambient_noise_key(_wc(key), 7) is not None) == foreign


def test_runtime_drops_foreign_key_keeps_noise():
    cfg = scoped_ambient_noise_config(_wc("recordings/99.wav"), 7)
    assert cfg == {"enabled": True, "volume": 0.5}


def test_runtime_keeps_own_key():
    wc = _wc("ambient-noise/7/1/x.wav")
    assert scoped_ambient_noise_config(wc, 7) is wc["ambient_noise_configuration"]


def test_runtime_without_org_drops_key():
    assert "storage_key" not in scoped_ambient_noise_config(
        _wc("ambient-noise/7/1/x.wav"), None
    )


async def test_duplicate_does_not_copy_foreign_object():
    from api.services.workflow import duplicate as dup

    source_def = SimpleNamespace(
        workflow_json={"nodes": [], "edges": []},
        template_context_variables=None,
        workflow_configurations=_wc("recordings/99.wav"),
    )
    source = SimpleNamespace(
        id=1, name="w", released_definition=source_def, call_disposition_codes=None
    )
    new = SimpleNamespace(id=2)
    db = SimpleNamespace(
        get_workflow=AsyncMock(return_value=source),
        get_draft_version=AsyncMock(return_value=None),
        create_workflow=AsyncMock(return_value=new),
        update_workflow=AsyncMock(return_value=new),
    )
    copy = AsyncMock(return_value=True)
    with (
        patch.object(dup, "db_client", db),
        patch.object(dup, "_copy_storage_object", copy),
    ):
        await dup.duplicate_workflow(1, organization_id=7, user_id=3)
    copy.assert_not_awaited()
    saved = db.update_workflow.await_args.kwargs["workflow_configurations"]
    assert "storage_key" not in saved["ambient_noise_configuration"]


# --- saving a workflow that already holds a foreign key (review round 2 R8) --


async def _workflow_with_draft_configs(db_session, tag, configs):
    user, _ = await db_session.get_or_create_user_by_provider_id(f"ambient_{tag}")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"ambient_org_{tag}", user.id
    )
    await db_session.update_user_selected_organization(user.id, org.id)
    user = await db_session.get_user_by_id(user.id)
    workflow = await db_session.create_workflow(
        name="ambient",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org.id,
    )
    await db_session.save_workflow_draft(
        workflow_id=workflow.id, workflow_configurations=configs
    )
    return user, workflow


async def test_save_with_unchanged_foreign_key_strips_it(
    test_client_factory, db_session
):
    """A workflow saved before the write-side check still holds a foreign key;
    the UI sends workflow_configurations back whole on every save. An
    unchanged key is dropped the way the runtime drops it, not a 403 on every
    save from then on."""
    stored = _wc("recordings/99.wav")
    user, workflow = await _workflow_with_draft_configs(db_session, "same", stored)

    async with test_client_factory(user) as client:
        r = await client.put(
            f"/api/v1/workflow/{workflow.id}",
            json={"workflow_configurations": _wc("recordings/99.wav")},
        )

    assert r.status_code == 200, r.text
    draft = await db_session.get_draft_version(workflow.id)
    assert draft.workflow_configurations["ambient_noise_configuration"] == {
        "enabled": True,
        "volume": 0.5,
    }


async def test_save_with_a_new_foreign_key_is_refused(test_client_factory, db_session):
    user, workflow = await _workflow_with_draft_configs(
        db_session, "new", _wc("recordings/99.wav")
    )

    async with test_client_factory(user) as client:
        r = await client.put(
            f"/api/v1/workflow/{workflow.id}",
            json={"workflow_configurations": _wc("recordings/100.wav")},
        )

    assert r.status_code == 403, r.text
    draft = await db_session.get_draft_version(workflow.id)
    assert draft.workflow_configurations == _wc("recordings/99.wav")
