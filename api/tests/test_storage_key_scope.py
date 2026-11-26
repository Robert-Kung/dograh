"""Org-scoped storage key checks reject non-canonical keys (review gate S8).

A bare prefix check passes ``knowledge_base/{own org}/../{other org}/x``; a
storage backend that normalizes paths would then read another org's object.
Every org-prefix check must reject ``..``/``.`` segments, empty segments
(``//``, leading or trailing ``/``) and backslashes.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.knowledge_base import router as kb_router
from api.routes.s3_signed_url import _extract_org_id_from_key
from api.services.auth.depends import get_user
from api.services.campaign.sources.csv import CSVSyncService

ORG = 42


def _bad_keys(prefix: str) -> list[str]:
    return [
        f"{prefix}/{ORG}/../7/file.csv",
        f"{prefix}/{ORG}/./file.csv",
        f"{prefix}/{ORG}//file.csv",
        f"{prefix}/{ORG}/sub\\..\\file.csv",
        f"{prefix}/{ORG}/",
        # round 2 S4: forms a decoding/normalizing backend turns into traversal
        f"{prefix}/{ORG}/%2e%2e/7/file.csv",
        f"{prefix}/{ORG}/\uff0e\uff0e/7/file.csv",
        f"{prefix}/{ORG}/a%2fb.csv",
        f"{prefix}/{ORG}/file.csv\x00",
    ]


@pytest.mark.parametrize("key", _bad_keys("campaigns") + _bad_keys("knowledge_base"))
def test_signed_url_org_extraction_rejects_non_canonical_keys(key):
    assert _extract_org_id_from_key(key) is None


@pytest.mark.parametrize("key", _bad_keys("knowledge_base"))
def test_process_document_rejects_non_canonical_key(key):
    app = FastAPI()
    app.include_router(kb_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1, selected_organization_id=ORG
    )
    with patch("api.routes.knowledge_base.db_client") as mock_db:
        mock_db.create_document = AsyncMock()
        response = TestClient(app).post(
            "/knowledge-base/process-document",
            json={"document_uuid": "u-1", "s3_key": key},
        )
    assert response.status_code == 403
    assert not mock_db.create_document.called


@pytest.mark.parametrize("key", _bad_keys("campaigns"))
async def test_csv_source_rejects_non_canonical_key(key):
    service = CSVSyncService()
    with patch.object(service, "_fetch_csv_data", new=AsyncMock()) as fetch:
        result = await service.validate_source(key, organization_id=ORG)
    assert not result.is_valid
    assert fetch.await_count == 0


async def test_csv_source_accepts_canonical_own_key():
    service = CSVSyncService()
    rows = [["phone_number"], ["+886900000000"]]
    with patch.object(service, "_fetch_csv_data", new=AsyncMock(return_value=rows)):
        result = await service.validate_source(
            f"campaigns/{ORG}/abc/source.csv", organization_id=ORG
        )
    assert result.is_valid, result.error


def test_org_segment_is_ascii_digits_only():
    assert _extract_org_id_from_key("campaigns/\uff11/x.csv") is None
    assert _extract_org_id_from_key(f"campaigns/{ORG}/x.csv") == ORG


def test_free_form_names_still_accepted():
    from api.utils.storage_key import is_org_scoped_key

    assert is_org_scoped_key(
        f"knowledge_base/{ORG}/u-1/客服手冊 v2.pdf", "knowledge_base", ORG
    )
