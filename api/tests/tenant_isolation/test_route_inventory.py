"""Route inventory (mt-s1-foundation 8.3) — tenant-isolation〈跨租戶隔離測試〉.

The real run checks the live app against ``route_scope.py``; each rule also has
a synthetic red-light case so a rule that stops firing is caught.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from api.tests.tenant_isolation import route_scope
from api.tests.tenant_isolation.conftest import ROUTE_HITS_ENV
from api.tests.tenant_isolation.route_inventory import (
    check_classes,
    check_coverage,
    check_org_scope_hits,
    check_org_scope_refs,
    check_probe_identity,
    check_probe_snapshot,
    enumerate_routes,
    load_snapshot,
    referenced_nodes,
)

API_DIR = Path(__file__).resolve().parents[2]
PYTEST_ARGS = ["-p", "no:cacheprovider", "-o", "addopts=--import-mode=importlib"]


@pytest.fixture(scope="module")
def live_routes():
    from api.app import app

    return enumerate_routes(app)


def _pytest(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *PYTEST_ARGS, *args],
        check=False,
        cwd=API_DIR,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=600,
    )


# --- real run ----------------------------------------------------------------


@pytest.mark.scenario("tenant-isolation:新路由未歸類")
def test_every_route_is_classified_once(live_routes):
    problems = check_classes(
        route_scope.ORG_SCOPED, route_scope.EXEMPT, route_scope.PROBED
    ) + check_coverage(
        live_routes, route_scope.ORG_SCOPED, route_scope.EXEMPT, route_scope.PROBED
    )
    assert not problems, "\n".join(problems)


@pytest.mark.scenario("tenant-isolation:新路由不得列入探針類")
def test_probe_class_stays_within_snapshot():
    problems = check_probe_snapshot(route_scope.PROBED, load_snapshot())
    assert not problems, "\n".join(problems)


@pytest.mark.scenario("tenant-isolation:無身分端點不得列入探針類")
def test_probe_class_routes_carry_identity(live_routes):
    problems = check_probe_identity(route_scope.PROBED, live_routes)
    assert not problems, "\n".join(problems)


@pytest.mark.scenario("tenant-isolation:組織範圍標籤不連真測試")
def test_org_scoped_entries_cite_collected_tests_that_hit_them(tmp_path):
    collected = _pytest("--collect-only", "-q", "tests/tenant_isolation")
    assert collected.returncode == 0, collected.stdout[-3000:] + collected.stderr
    node_ids = [ln for ln in collected.stdout.splitlines() if "::" in ln]
    problems = check_org_scope_refs(route_scope.ORG_SCOPED, node_ids)
    assert not problems, "\n".join(problems)

    hits_file = tmp_path / "hits.jsonl"
    run = _pytest(
        "-q",
        *referenced_nodes(route_scope.ORG_SCOPED),
        env={ROUTE_HITS_ENV: str(hits_file)},
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr
    hits: dict[str, set[str]] = {}
    for line in hits_file.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        hits.setdefault(rec["node"], set()).update(rec["hits"])
    problems = check_org_scope_hits(route_scope.ORG_SCOPED, hits)
    assert not problems, "\n".join(problems)


# --- red lights: each rule fires on a synthetic violation --------------------

ROUTE = "GET /api/v1/x/{id}"
TEST = "tests/tenant_isolation/test_x.py::test_x"


def test_red_unclassified_and_stale_routes():
    assert check_coverage([ROUTE], {}, {}, []) == [f"{ROUTE}: unclassified route"]
    assert check_coverage([], {}, {ROUTE: "r"}, []) == [
        f"{ROUTE}: classified but no such route"
    ]


def test_red_double_class_empty_reason_and_uncited_org_scope():
    assert check_classes({}, {ROUTE: "r"}, [ROUTE]) == [
        f"{ROUTE}: listed in both (b) and (c)"
    ]
    assert check_classes({}, {ROUTE: " "}, []) == [
        f"{ROUTE}: (b) exemption without a reason"
    ]
    assert check_classes({ROUTE: ()}, {}, []) == [
        f"{ROUTE}: (a) entry cites no isolation test"
    ]


def test_red_org_scope_cites_missing_test():
    missing = check_org_scope_refs({ROUTE: [TEST]}, ["tests/tenant_isolation/a.py::b"])
    assert missing == [f"{ROUTE}: {TEST} not collected by pytest"]
    outside = check_org_scope_refs({ROUTE: ["tests/x.py::t"]}, ["tests/x.py::t"])
    assert outside == [f"{ROUTE}: tests/x.py::t is not under tests/tenant_isolation/"]
    selfref = "tests/tenant_isolation/test_route_inventory.py::t"
    assert check_org_scope_refs({ROUTE: [selfref]}, [selfref]) == [
        f"{ROUTE}: {selfref} is the inventory test itself"
    ]
    # parametrized ids match by base name
    assert check_org_scope_refs({ROUTE: [TEST]}, [TEST + "[1]"]) == []


def test_red_org_scope_test_never_hits_route():
    other = {TEST: {"GET /api/v1/y"}}
    assert check_org_scope_hits({ROUTE: [TEST]}, other) == [
        f"{ROUTE}: {TEST} ran but never hit this route"
    ]
    assert check_org_scope_hits({ROUTE: [TEST]}, {TEST + "[1]": {ROUTE}}) == []


def test_red_probe_route_outside_snapshot():
    assert check_probe_snapshot([ROUTE], ["GET /api/v1/y"]) == [
        f"{ROUTE}: (c) outside the frozen probe snapshot — prove it in (a) or exempt it in (b)"
    ]


def test_red_probe_route_without_identity():
    problems = check_probe_identity([ROUTE], {ROUTE: {"verify_signature"}})
    assert len(problems) == 1 and problems[0].startswith(f"{ROUTE}: (c) route has no")
    assert check_probe_identity([ROUTE], {ROUTE: {"get_user"}}) == []


def test_hit_recorder_sees_route_templates(tmp_path, monkeypatch):
    """The conftest recorder logs the matched template, not the concrete URL."""
    probe = tmp_path / "test_probe_hits.py"
    probe.write_text(
        "from fastapi import FastAPI\n"
        "from fastapi.testclient import TestClient\n"
        "app = FastAPI()\n"
        "@app.get('/items/{item_id}')\n"
        "def item(item_id: int):\n"
        "    return {}\n"
        "def test_hit():\n"
        "    TestClient(app).get('/items/7')\n",
        encoding="utf-8",
    )
    conftest = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    hook = conftest[conftest.index("# --- route hit log") :]
    (tmp_path / "conftest.py").write_text(
        "import json, os\nimport pytest\n"
        "from starlette.routing import Route, WebSocketRoute\n" + hook,
        encoding="utf-8",
    )
    hits_file = tmp_path / "hits.jsonl"
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            "-c",
            os.devnull,
            str(probe),
        ],
        check=False,
        cwd=tmp_path,
        env={**os.environ, ROUTE_HITS_ENV: str(hits_file)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads(hits_file.read_text(encoding="utf-8"))
    assert rec["hits"] == ["GET /items/{item_id}"]
