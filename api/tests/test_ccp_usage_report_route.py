"""ccp W4b internal usage-report route: auth, validation, error mapping, shape."""

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from api.routes import ccp_usage_report as route
from api.services.ccp import usage_report as ur

SECRET = "test-dograh-devops-secret"
PATH = "/api/v1/internal/usage-report"
GOOD = {"from": "2021-11-01", "to": "2021-11-02", "timezone": "Asia/Taipei"}

REPORT = {
    "calls": 3,
    "ai_seconds": 120.5,
    "outcomes": {
        "ai_completed": 2,
        "transferred": 1,
        "transfer_failed": 0,
        "system_error": 0,
        "unrecorded": 0,
    },
    "daily": [
        {"date": "2021-11-01", "calls": 3, "ai_completed": 2, "transferred": 1},
        {"date": "2021-11-02", "calls": 0, "ai_completed": 0, "transferred": 0},
    ],
    "categories": {
        "billing_inquiry": {"calls": 1, "ai_completed": 1, "transferred": 0}
    },
    "unclassified": {"calls": 2, "ai_completed": 1, "transferred": 1},
}


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def fake_build(query, **_):
        seen.append(query)
        return REPORT

    monkeypatch.setattr(ur, "build_usage_report", fake_build)
    return seen


def _client(monkeypatch, configured=SECRET) -> TestClient:
    monkeypatch.setattr("api.constants.DOGRAH_DEVOPS_SECRET", configured)
    app = FastAPI()
    app.include_router(route.router, prefix="/api/v1")
    return TestClient(app)


def _get(client, params=GOOD, secret: str | bytes | None = SECRET):
    headers = {} if secret is None else {"X-Dograh-Devops-Secret": secret}
    return client.get(PATH, params=params, headers=headers)


def test_unconfigured_secret_is_503(monkeypatch, calls):
    assert _get(_client(monkeypatch, configured=None)).status_code == 503
    assert calls == []


@pytest.mark.parametrize(
    "secret", [None, "wrong", "test-dograh-devops-secrét".encode()]
)
def test_bad_secret_is_403_without_querying(monkeypatch, calls, secret):
    assert _get(_client(monkeypatch), secret=secret).status_code == 403
    assert calls == []


def test_auth_runs_before_param_validation(monkeypatch, calls):
    resp = _get(_client(monkeypatch), params={"from": "nope"}, secret="wrong")
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "override",
    [
        {"timezone": "localtime"},
        {"timezone": "../x"},
        {"timezone": ""},
        {"timezone": "Mars/Olympus"},
        {"from": "2021-01-01", "to": "2021-04-03"},
        {"from": "0001-01-01", "to": "0001-03-01"},
        {"from": "not-a-date"},
        {"codes": ["Upper"]},
    ],
)
def test_invalid_input_is_422(monkeypatch, calls, override):
    resp = _get(_client(monkeypatch), params={**GOOD, **override})
    assert resp.status_code == 422
    assert calls == []


def test_missing_timezone_is_422(monkeypatch, calls):
    params = {k: v for k, v in GOOD.items() if k != "timezone"}
    assert _get(_client(monkeypatch), params=params).status_code == 422


def test_codes_reach_the_service_as_a_list(monkeypatch, calls):
    params = {**GOOD, "codes": ["billing_inquiry", "general_inquiry"]}
    assert _get(_client(monkeypatch), params=params).status_code == 200
    assert calls[0] == ur.UsageReportQuery(
        date(2021, 11, 1),
        date(2021, 11, 2),
        "Asia/Taipei",
        ("billing_inquiry", "general_inquiry"),
    )


def test_timeout_is_503(monkeypatch):
    async def timeout(query, **_):
        raise ur.UsageReportTimeout()

    monkeypatch.setattr(ur, "build_usage_report", timeout)
    resp = _get(_client(monkeypatch))
    assert resp.status_code == 503
    assert resp.json() == {"detail": "timeout"}


def test_failure_logs_type_only(monkeypatch):
    async def boom(query, **_):
        raise RuntimeError("row value 0912345678")

    monkeypatch.setattr(ur, "build_usage_report", boom)
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)))
    try:
        resp = _get(_client(monkeypatch))
    finally:
        logger.remove(sink)
    assert resp.status_code == 503
    assert "0912345678" not in resp.text
    assert any("RuntimeError" in line for line in lines)
    assert not any("0912345678" in line for line in lines)


def test_response_keys(monkeypatch, calls):
    body = _get(_client(monkeypatch)).json()
    assert set(body) == {
        "calls",
        "ai_seconds",
        "outcomes",
        "daily",
        "categories",
        "unclassified",
    }
    assert set(body["outcomes"]) == set(ur.OUTCOME_CLASSES)
    assert set(body["daily"][0]) == {"date", "calls", "ai_completed", "transferred"}
    assert set(body["unclassified"]) == {"calls", "ai_completed", "transferred"}


def test_registered_on_the_main_router():
    from api.routes.main import router as main_router

    paths = {r.path for r in main_router.routes}
    assert "/internal/usage-report" in paths
