"""ccp W4c: the recordings bucket has no anonymous policy; URLs are signed."""

from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from minio import Minio

from api.services.filesystem.minio import MinioFileSystem


@pytest.fixture
def bucket_calls(monkeypatch):
    calls = MagicMock()
    monkeypatch.setattr(Minio, "bucket_exists", lambda self, b: calls.exists(b) or True)
    monkeypatch.setattr(Minio, "make_bucket", lambda self, b: calls.make(b))
    monkeypatch.setattr(
        Minio, "delete_bucket_policy", lambda self, b: calls.delete_policy(b)
    )
    monkeypatch.setattr(
        Minio, "set_bucket_policy", lambda self, b, p: calls.set_policy(b, p)
    )
    return calls


def _fs():
    return MinioFileSystem(
        endpoint="minio:9000",
        access_key="ak",
        secret_key="sk",
        bucket_name="voice-audio",
        public_endpoint="https://files.example.com",
    )


def test_startup_removes_the_anonymous_policy(bucket_calls):
    _fs()
    bucket_calls.delete_policy.assert_called_once_with("voice-audio")
    bucket_calls.set_policy.assert_not_called()


def test_policy_removal_failure_does_not_break_startup(bucket_calls, monkeypatch):
    def boom(self, b):
        raise RuntimeError("restricted")

    monkeypatch.setattr(Minio, "delete_bucket_policy", boom)
    _fs()


async def test_get_url_is_signed_on_the_public_endpoint(bucket_calls):
    url = await _fs().aget_signed_url("recordings/1.wav", expiration=600, force_inline=True)
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    assert parts.scheme == "https" and parts.netloc == "files.example.com"
    assert parts.path == "/voice-audio/recordings/1.wav"
    assert q["X-Amz-Signature"] and q["X-Amz-Expires"] == ["600"]
    assert q["response-content-type"] == ["audio/wav"]


async def test_internal_url_is_signed_on_the_internal_endpoint(bucket_calls):
    url = await _fs().aget_signed_url("campaigns/x.csv", use_internal_endpoint=True)
    parts = urlsplit(url)
    assert parts.netloc == "minio:9000" and parts.scheme == "http"
    assert "X-Amz-Signature" in parse_qs(parts.query)


async def test_put_url_is_signed(bucket_calls):
    url = await _fs().aget_presigned_put_url("uploads/a.csv")
    q = parse_qs(urlsplit(url).query)
    assert q["X-Amz-Signature"] and q["X-Amz-Expires"] == ["900"]
