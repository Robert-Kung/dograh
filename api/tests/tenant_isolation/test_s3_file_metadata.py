"""``GET /s3/file-metadata`` is scoped to the run's organization (mt-s1-foundation 8.3b).

tenant-isolation〈跨租戶隔離測試〉 R1: before the fix a non-superuser got the
metadata of another organization's recording because the run lookup ran with
``require_workflow_run=False`` (not found → no raise → served from the default
storage), and ``voicemail_detections/`` keys skipped authorization entirely.
"""

from __future__ import annotations

import pytest

import api.routes.s3_signed_url as s3

URL = "/api/v1/s3/file-metadata"


@pytest.fixture
def fake_storage(monkeypatch):
    """Every key exists; metadata echoes the key so a leak is visible."""

    class _Storage:
        async def aget_file_metadata(self, key):
            return {"key": key, "size": 1234}

    storage = _Storage()
    monkeypatch.setattr(s3, "storage_fs", storage)
    monkeypatch.setattr(s3, "get_storage_for_backend", lambda backend: storage)
    return storage


@pytest.mark.scenario("tenant-isolation:探針抓到傳了組織卻不擋的路由")
@pytest.mark.parametrize(
    "key_of", ["recordings/{}.wav", "recordings/{}/user.wav", "transcripts/{}.txt"]
)
async def test_other_org_run_metadata_is_denied(
    test_client_factory, two_tenants, fake_storage, key_of
):
    a, b = two_tenants.a, two_tenants.b
    async with test_client_factory(b.user) as client:
        other = await client.get(URL, params={"key": key_of.format(a.run.id)})
        own = await client.get(URL, params={"key": key_of.format(b.run.id)})
    assert other.status_code == 403
    assert str(a.run.id) not in other.text
    assert own.status_code == 200
    assert own.json()["metadata"]["key"] == key_of.format(b.run.id)


@pytest.mark.scenario("tenant-isolation:探針抓到傳了組織卻不擋的路由")
async def test_voicemail_metadata_is_superuser_only(
    test_client_factory, two_tenants, fake_storage
):
    async with test_client_factory(two_tenants.b.user) as client:
        r = await client.get(URL, params={"key": "voicemail_detections/x.wav"})
    assert r.status_code == 403
