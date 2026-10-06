"""ccp W4b usage report aggregation — against the real test database.

Rows are inserted with explicit ``created_at`` in 2021 so nothing else in the
test DB falls into the queried ranges; ``now`` is pinned accordingly.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from api.db.models import OrganizationModel, UserModel, WorkflowRunModel
from api.services.ccp import usage_report as ur

NOW = datetime(2021, 11, 20, 4, 0, tzinfo=UTC)
TZ = "Asia/Taipei"
CODES = ["general_inquiry", "billing_inquiry"]


@pytest.fixture
async def workflow(db_session, async_session):
    org = OrganizationModel(provider_id="test-org-usage-report")
    async_session.add(org)
    await async_session.flush()
    user = UserModel(
        provider_id="test-user-usage-report", selected_organization_id=org.id
    )
    async_session.add(user)
    await async_session.flush()
    return await db_session.create_workflow(
        name="Usage Report Workflow",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org.id,
    )


@pytest.fixture
def add_run(workflow, async_session):
    async def _add(
        created_at: datetime,
        *,
        outcome: str | None = "ai_completed",
        completed: bool = True,
        mode: str = "livekit",
        call_type: str = "inbound",
        seconds=60,
        disposition: str | None = None,
    ):
        run = WorkflowRunModel(
            name="usage-report-run",
            workflow_id=workflow.id,
            mode=mode,
            call_type=call_type,
            is_completed=completed,
            created_at=created_at,
            annotations={"call_outcome": outcome} if outcome else {},
            usage_info={} if seconds is None else {"call_duration_seconds": seconds},
            gathered_context=(
                {"mapped_call_disposition": disposition} if disposition else {}
            ),
        )
        async_session.add(run)
        await async_session.flush()

    return _add


def _query(date_from: date, date_to: date, codes=CODES) -> ur.UsageReportQuery:
    return ur.validate_query(date_from, date_to, TZ, list(codes), now=NOW)


def _at(y, m, d, hh=2) -> datetime:
    # 02:00 UTC = 10:00 Taipei, safely inside the local day.
    return datetime(y, m, d, hh, tzinfo=UTC)


@pytest.mark.asyncio
async def test_scope_excludes_test_calls_outbound_and_in_progress(add_run):
    for _ in range(3):
        await add_run(_at(2021, 11, 3))
    await add_run(_at(2021, 11, 3), mode="smallwebrtc")
    await add_run(_at(2021, 11, 3), call_type="outbound")
    await add_run(NOW - timedelta(minutes=10), outcome=None, completed=False)

    report = await ur.build_usage_report(
        _query(date(2021, 11, 1), date(2021, 11, 20)), now=NOW
    )

    assert report["calls"] == 3


@pytest.mark.asyncio
async def test_safetynet_and_stale_runs_are_counted(add_run):
    # Engine-free safetynet: outcome written, never marked completed.
    await add_run(_at(2021, 11, 4), outcome="transferred:safetynet", completed=False)
    await add_run(_at(2021, 11, 4), outcome="safetynet_terminated", completed=False)
    # Worker killed: no flag, no outcome, created 7 h ago.
    await add_run(NOW - timedelta(hours=7), outcome=None, completed=False)
    # A failed transfer is not terminal — still in progress, not counted.
    await add_run(
        NOW - timedelta(minutes=5), outcome="transfer_failed:no_agent", completed=False
    )

    report = await ur.build_usage_report(
        _query(date(2021, 11, 1), date(2021, 11, 20)), now=NOW
    )

    assert report["calls"] == 3
    assert report["outcomes"]["system_error"] == 2
    assert report["outcomes"]["transferred"] == 0
    assert report["outcomes"]["unrecorded"] == 1


@pytest.mark.asyncio
async def test_outcome_classes_sum_to_calls(add_run):
    for _ in range(6):
        await add_run(_at(2021, 11, 5))
    for _ in range(2):
        await add_run(_at(2021, 11, 5), outcome="transferred:press0")
    await add_run(_at(2021, 11, 5), outcome="transfer_failed:no_agent")
    await add_run(_at(2021, 11, 5), outcome=None)
    await add_run(_at(2021, 11, 5), outcome="transfer_failed:press0_not_installed")
    await add_run(_at(2021, 11, 5), outcome="transfer_failed:config_unresolvable")
    await add_run(_at(2021, 11, 5), outcome="something_new")

    report = await ur.build_usage_report(
        _query(date(2021, 11, 5), date(2021, 11, 5)), now=NOW
    )

    assert report["outcomes"] == {
        "ai_completed": 6,
        "transferred": 2,
        "transfer_failed": 1,
        "system_error": 0,
        "unrecorded": 4,
    }
    assert sum(report["outcomes"].values()) == report["calls"] == 13
    assert report["daily"] == [
        {"date": "2021-11-05", "calls": 13, "ai_completed": 6, "transferred": 2}
    ]


@pytest.mark.asyncio
async def test_day_boundary_follows_deployment_timezone(add_run):
    # 16:30 UTC on Oct 31 is 00:30 on Nov 1 in Taipei.
    await add_run(datetime(2021, 10, 31, 16, 30, tzinfo=UTC))
    await add_run(datetime(2021, 10, 31, 15, 59, tzinfo=UTC))

    october = await ur.build_usage_report(
        _query(date(2021, 10, 1), date(2021, 10, 31)), now=NOW
    )
    november = await ur.build_usage_report(
        _query(date(2021, 11, 1), date(2021, 11, 20)), now=NOW
    )

    assert october["calls"] == 1
    assert october["daily"][-1]["calls"] == 1
    assert november["calls"] == 1
    assert november["daily"][0] == {
        "date": "2021-11-01",
        "calls": 1,
        "ai_completed": 1,
        "transferred": 0,
    }


@pytest.mark.asyncio
async def test_daily_series_has_no_gaps(add_run):
    await add_run(_at(2021, 10, 3))

    report = await ur.build_usage_report(
        _query(date(2021, 10, 1), date(2021, 10, 7)), now=NOW
    )

    assert [d["date"] for d in report["daily"]] == [
        f"2021-10-0{i}" for i in range(1, 8)
    ]
    assert [d["calls"] for d in report["daily"]] == [0, 0, 1, 0, 0, 0, 0]


@pytest.mark.asyncio
async def test_seconds_tolerate_dirty_values(add_run):
    await add_run(_at(2021, 11, 6), seconds=90.5)
    await add_run(_at(2021, 11, 6), seconds="abc")
    await add_run(_at(2021, 11, 6), seconds=None)
    await add_run(_at(2021, 11, 6), seconds=-30)
    await add_run(_at(2021, 11, 6), seconds=True)

    report = await ur.build_usage_report(
        _query(date(2021, 11, 6), date(2021, 11, 6)), now=NOW
    )

    assert report["calls"] == 5
    assert report["ai_seconds"] == 90.5


@pytest.mark.asyncio
async def test_categories_whitelist_folds_in_sql(add_run):
    free_text = "客戶 0912345678 詢問帳單"
    await add_run(_at(2021, 11, 7), disposition=" Billing_Inquiry ")
    await add_run(
        _at(2021, 11, 7), disposition="billing_inquiry", outcome="transferred:voice"
    )
    await add_run(_at(2021, 11, 7), disposition="user_hangup")
    await add_run(_at(2021, 11, 7), disposition=free_text)
    await add_run(_at(2021, 11, 7))

    report = await ur.build_usage_report(
        _query(date(2021, 11, 7), date(2021, 11, 7)), now=NOW
    )

    assert report["categories"] == {
        "general_inquiry": {"calls": 0, "ai_completed": 0, "transferred": 0},
        "billing_inquiry": {"calls": 2, "ai_completed": 1, "transferred": 1},
    }
    assert report["unclassified"] == {"calls": 3, "ai_completed": 3, "transferred": 0}
    assert free_text not in str(report)
    assert "user_hangup" not in str(report)


@pytest.mark.asyncio
async def test_no_codes_puts_everything_in_unclassified(add_run):
    await add_run(_at(2021, 11, 8), disposition="billing_inquiry")

    report = await ur.build_usage_report(
        _query(date(2021, 11, 8), date(2021, 11, 8), codes=[]), now=NOW
    )

    assert report["categories"] == {}
    assert report["unclassified"]["calls"] == 1


@pytest.mark.asyncio
async def test_statement_timeout_is_recognised(async_session):
    """The real asyncpg error for a cancelled statement maps to a timeout."""
    await async_session.execute(text("SET LOCAL statement_timeout = '10ms'"))
    with pytest.raises(DBAPIError) as caught:
        await async_session.execute(text("SELECT pg_sleep(1)"))
    assert ur._is_statement_timeout(caught.value)


@pytest.mark.asyncio
async def test_timeout_raises_usage_report_timeout(db_session, monkeypatch):
    monkeypatch.setattr(ur, "STATEMENT_TIMEOUT", "1ms")
    monkeypatch.setattr(
        ur,
        "_SQL",
        text(
            "SELECT 1 AS day_idx, NULL AS code, 'ai_completed' AS cls,"
            " 1 AS n, 0 AS secs FROM pg_sleep(1)"
        ),
    )
    with pytest.raises(ur.UsageReportTimeout):
        await ur.build_usage_report(
            _query(date(2021, 11, 1), date(2021, 11, 1)), now=NOW
        )


# --- input validation (no DB) ---


@pytest.mark.parametrize(
    "date_from,date_to,ok",
    [
        (date(2021, 1, 1), date(2021, 4, 2), True),  # exactly 92 days
        (date(2021, 1, 1), date(2021, 4, 3), False),  # 93 days
        (date(2020, 1, 1), date(2020, 1, 1), True),
        (date(2019, 12, 31), date(2020, 1, 1), False),
        (date(1, 1, 1), date(1, 3, 1), False),
        (date(2021, 11, 2), date(2021, 11, 1), False),  # reversed
        (date(2021, 11, 20), date(2021, 11, 20), True),  # today in Taipei
        (date(2021, 11, 20), date(2021, 11, 21), False),  # future
    ],
)
def test_period_rules(date_from, date_to, ok):
    if ok:
        ur.validate_query(date_from, date_to, TZ, [], now=NOW)
    else:
        with pytest.raises(ur.UsageReportInvalid):
            ur.validate_query(date_from, date_to, TZ, [], now=NOW)


def test_today_is_judged_in_the_deployment_timezone():
    # 17:00 UTC Nov 20 is already Nov 21 in Taipei.
    late = datetime(2021, 11, 20, 17, 0, tzinfo=UTC)
    ur.validate_query(date(2021, 11, 21), date(2021, 11, 21), TZ, [], now=late)


@pytest.mark.parametrize(
    "tz", ["Mars/Olympus", "localtime", "Factory", "posixrules", "../x", ""]
)
def test_timezone_must_be_geographic(tz):
    with pytest.raises(ur.UsageReportInvalid):
        ur.validate_query(date(2021, 11, 1), date(2021, 11, 1), tz, [], now=NOW)


@pytest.mark.parametrize(
    "codes",
    [
        ["Billing"],
        ["a" * 41],
        ["has space"],
        [""],
        ["dup", "dup"],
        [f"c{i}" for i in range(21)],
    ],
)
def test_codes_rules(codes):
    with pytest.raises(ur.UsageReportInvalid):
        ur.validate_query(date(2021, 11, 1), date(2021, 11, 1), TZ, codes, now=NOW)


def test_twenty_codes_accepted():
    ur.validate_query(
        date(2021, 11, 1), date(2021, 11, 1), TZ, [f"c{i}" for i in range(20)], now=NOW
    )


def test_boundaries_are_local_midnights():
    q = _query(date(2021, 11, 1), date(2021, 11, 2))
    assert ur.day_boundaries(q) == [
        datetime(2021, 10, 31, 16, tzinfo=UTC),
        datetime(2021, 11, 1, 16, tzinfo=UTC),
        datetime(2021, 11, 2, 16, tzinfo=UTC),
    ]
