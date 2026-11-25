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
