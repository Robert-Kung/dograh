"""ccp W4c internal call-records routes: auth, fixed error codes, 404s, shapes."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import ccp_call_records as route
from api.services.ccp import call_records as cr

SECRET = "test-dograh-call-records-secret"
DEVOPS = "test-dograh-devops-secret"
BASE = "/api/v1/internal/call-records"
BODY = {"from": "2021-11-01", "to": "2021-11-02", "timezone": "Asia/Taipei"}

LIST = {
    "total": 1,
    "page": 1,
    "pages": 1,
    "page_size": 50,
    "rows": [
        {
            "run_id": 5,
            "started_at": "2021-11-01T02:00:00+00:00",
            "ai_seconds": 60.0,
            "outcome": "ai_completed",
            "category": None,
            "recording_status": "none",
            "caller_masked": "***5678",
        }
    ],
    "recording_enabled": False,
    "caller_search": "full",
    "caller_coverage_from": "2021-11-01",
    "key_mismatch": False,
}
DETAIL = {
    "run_id": 5,
    "started_at": "2021-11-01T02:00:00+00:00",
    "ai_seconds": 60.0,
    "outcome": "transferred",
    "handed_off": True,
    "category": "billing_inquiry",
    "caller_masked": None,
    "did": "+886212345678",
    "recording_status": "available",
    "transcript_status": "available",
    "retention": {"audio_days": 180, "transcript": "never"},
    "segments": [
        {
            "at": "2021-11-01T02:00:01+00:00",
            "offset_ms": 1000,
            "seekable": True,
            "speaker": "ai",
            "text": "您好",
            "source": "ai_leg",
        }
    ],
    "segments_truncated": False,
    "extracted": [{"key": "name", "value": "王"}],
    "extracted_truncated": False,
}


@pytest.fixture
def svc(monkeypatch):
    seen = {"query": [], "detail": [], "audio": []}

    async def fake_query(query, *, timezone, **_):
        seen["query"].append(query)
        return LIST

    async def fake_get(run_id, codes, **_):
        seen["detail"].append((run_id, codes))
        return DETAIL if run_id == 5 else None

    monkeypatch.setattr(cr, "query_calls", fake_query)
    monkeypatch.setattr(cr, "get_call", fake_get)
    return seen


def _client(monkeypatch, configured=SECRET) -> TestClient:
    monkeypatch.setattr("api.constants.DOGRAH_CALL_RECORDS_SECRET", configured)
    monkeypatch.setattr("api.constants.DOGRAH_DEVOPS_SECRET", DEVOPS)
    app = FastAPI()
    app.include_router(route.router, prefix="/api/v1")
    return TestClient(app)


def _h(secret=SECRET):
    return {} if secret is None else {"X-Dograh-Call-Records-Secret": secret}


def test_unconfigured_secret_is_503(monkeypatch, svc):
    r = _client(monkeypatch, configured=None).post(
        f"{BASE}/query", json=BODY, headers=_h()
    )
    assert r.status_code == 503 and svc["query"] == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Dograh-Call-Records-Secret": "wrong"},
        {"X-Dograh-Call-Records-Secret": "test-dograh-call-records-secrét".encode()},
        {"X-Dograh-Devops-Secret": DEVOPS},  # the devops secret is not accepted
        {"X-Dograh-Call-Records-Secret": DEVOPS},
    ],
)
def test_bad_secret_is_403(monkeypatch, svc, headers):
    client = _client(monkeypatch)
    assert client.post(f"{BASE}/query", json=BODY, headers=headers).status_code == 403
    assert client.get(f"{BASE}/5", headers=headers).status_code == 403
    assert client.get(f"{BASE}/5/audio", headers=headers).status_code == 403
    assert svc == {"query": [], "detail": [], "audio": []}


def test_auth_runs_before_body_validation(monkeypatch, svc):
    r = _client(monkeypatch).post(f"{BASE}/query", content=b"{", headers=_h("wrong"))
    assert r.status_code == 403


def test_query_shape(monkeypatch, svc):
    r = _client(monkeypatch).post(
        f"{BASE}/query",
        json={**BODY, "codes": ["billing_inquiry"], "category": "billing_inquiry"},
        headers=_h(),
    )
    assert r.status_code == 200
    assert set(r.json()) == set(LIST)
    assert set(r.json()["rows"][0]) == set(LIST["rows"][0])
    assert svc["query"][0].category == "billing_inquiry"


@pytest.mark.parametrize(
    "body,code",
    [
        ({**BODY, "caller": "０９１２３４５６７８"}, "caller_invalid"),
        ({**BODY, "from": "2021-01-01", "to": "2021-11-02"}, "period_invalid"),
        ({**BODY, "outcome": "<script>"}, "query_invalid"),
        ({**BODY, "unknown": 1}, "query_invalid"),
        ({**BODY, "page": "x' OR 1=1"}, "query_invalid"),
        (
            {"from": "not-a-date", "to": "2021-11-02", "timezone": "Asia/Taipei"},
            "query_invalid",
        ),
        ([1, 2], "query_invalid"),
    ],
)
def test_query_422_is_a_fixed_code_without_echo(monkeypatch, svc, body, code):
    r = _client(monkeypatch).post(f"{BASE}/query", json=body, headers=_h())
    assert r.status_code == 422
    assert r.json() == {"detail": code}
    assert svc["query"] == []


def test_query_non_json_is_422(monkeypatch, svc):
    r = _client(monkeypatch).post(f"{BASE}/query", content=b"nope", headers=_h())
    assert r.status_code == 422 and r.json() == {"detail": "query_invalid"}


def test_caller_search_disabled(monkeypatch, svc):
    monkeypatch.delenv("CALLER_NUMBER_HMAC_KEY", raising=False)
    r = _client(monkeypatch).post(
        f"{BASE}/query", json={**BODY, "caller": "0912345678"}, headers=_h()
    )
    assert r.status_code == 422 and r.json() == {"detail": "caller_search_disabled"}


@pytest.mark.parametrize(
    "exc", [cr.CallRecordsTimeout(), RuntimeError("row 0912345678")]
)
def test_service_failure_is_503_without_detail(monkeypatch, exc):
    async def boom(*a, **k):
        raise exc

    monkeypatch.setattr(cr, "query_calls", boom)
    r = _client(monkeypatch).post(f"{BASE}/query", json=BODY, headers=_h())
    assert r.status_code == 503 and "0912345678" not in r.text


def test_detail_shape_and_codes(monkeypatch, svc):
    r = _client(monkeypatch).get(
        f"{BASE}/5", params={"codes": ["billing_inquiry"]}, headers=_h()
    )
    assert r.status_code == 200
    assert set(r.json()) == set(DETAIL)
    assert set(r.json()["segments"][0]) == set(DETAIL["segments"][0])
    assert svc["detail"] == [(5, ("billing_inquiry",))]


@pytest.mark.parametrize(
    "rid", ["6", "9999999999", "2147483648", "abc", "0", "-1", "01"]
)
def test_detail_404s(monkeypatch, svc, rid):
    r = _client(monkeypatch).get(f"{BASE}/{rid}", headers=_h())
    assert r.status_code == 404


def test_detail_bad_codes_422(monkeypatch, svc):
    r = _client(monkeypatch).get(f"{BASE}/5", params={"codes": ["Bad"]}, headers=_h())
    assert r.status_code == 422 and r.json() == {"detail": "query_invalid"}


# --- audio ---


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self, n):
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk

    def close(self):
        pass

    def release_conn(self):
        pass


@pytest.fixture
def audio(monkeypatch):
    blob = b"RIFF" + bytes(2000)

    async def fake_open(run_id, range_header, **_):
        if run_id != 5:
            return None
        span = cr.parse_range(range_header, len(blob))
        offset, length = span if span else (0, len(blob))
        await cr._audio_slots.acquire()
        return cr.AudioStream(
            206 if span else 200,
            len(blob),
            offset,
            length,
            _Resp(blob[offset : offset + length]),
        )

    monkeypatch.setattr(cr, "open_audio", fake_open)
    return blob


def test_audio_206(monkeypatch, audio):
    r = _client(monkeypatch).get(
        f"{BASE}/5/audio", headers={**_h(), "Range": "bytes=100-"}
    )
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-{len(audio) - 1}/{len(audio)}"
    assert r.headers["content-length"] == str(len(audio) - 100)
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["cache-control"] == "no-store"
    assert r.content == audio[100:]


def test_audio_200_and_404(monkeypatch, audio):
    client = _client(monkeypatch)
    assert client.get(f"{BASE}/5/audio", headers=_h()).content == audio
    assert client.get(f"{BASE}/6/audio", headers=_h()).status_code == 404
    assert client.get(f"{BASE}/abc/audio", headers=_h()).status_code == 404


def test_audio_416(monkeypatch, audio):
    r = _client(monkeypatch).get(
        f"{BASE}/5/audio", headers={**_h(), "Range": "bytes=0-10,20-30"}
    )
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(audio)}"


def test_audio_busy_is_503(monkeypatch):
    async def busy(*a, **k):
        raise cr.AudioBusy()

    monkeypatch.setattr(cr, "open_audio", busy)
    assert _client(monkeypatch).get(f"{BASE}/5/audio", headers=_h()).status_code == 503


def test_audio_slots_return_after_responses(monkeypatch, audio):
    client = _client(monkeypatch)
    for _ in range(cr.AUDIO_CONCURRENCY * 2):
        assert client.get(f"{BASE}/5/audio", headers=_h()).status_code == 200
    assert not cr._audio_slots.locked()
