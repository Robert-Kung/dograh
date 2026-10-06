"""AI reception usage report for the platform console (ccp W4b).

Aggregates LIVEKIT inbound workflow runs over a date range into counts only:
totals, five outcome classes, AI seconds, a per-day series and per-category
counts for a caller-supplied code whitelist. No per-call value — run id,
timestamp, number, transcript or a disposition string outside the whitelist —
ever leaves this module: unknown dispositions fold to NULL inside the SQL.

Day boundaries are computed here with ``zoneinfo`` (Postgres' tz database
never participates) and bucketed with ``width_bucket``. The query runs under a
5 s statement timeout because it shares the connection pool with live calls.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from api.db import db_client
from api.services.ccp import call_scope as cs
from api.services.ccp.call_scope import MAX_CALL_SECONDS as MAX_CALL_SECONDS
from api.services.ccp.call_scope import OUTCOME_CLASSES

MAX_DAYS = 92
STATEMENT_TIMEOUT = "5s"

UsageReportInvalid = cs.ScopeInvalid


class UsageReportTimeout(Exception):
    """The statement timeout fired."""


@dataclass(frozen=True)
class UsageReportQuery:
    date_from: date
    date_to: date
    timezone: str
    codes: tuple[str, ...]


def validate_query(
    date_from: date,
    date_to: date,
    timezone: str,
    codes: list[str],
    *,
    now: datetime | None = None,
) -> UsageReportQuery:
    cs.validate_period(date_from, date_to, timezone, max_days=MAX_DAYS, now=now)
    return UsageReportQuery(date_from, date_to, timezone, cs.validate_codes(codes))


def day_boundaries(query: UsageReportQuery) -> list[datetime]:
    """UTC instants of each local midnight from ``date_from`` to ``date_to + 1``."""
    return cs.day_boundaries(query.date_from, query.date_to, query.timezone)


# Scope, outcome classes and category folding are shared with call records
# (ccp W4c) so a filtered list always matches the report cell.
_SQL = text(
    f"""
WITH scoped AS (
    SELECT
        width_bucket(created_at, CAST(:bounds AS timestamptz[])) AS day_idx,
        annotations->>'call_outcome' AS outcome,
        {cs.seconds_expr()} AS secs,
        {cs.category_expr()} AS code
    FROM workflow_runs
    WHERE created_at >= :start AND created_at < :end
      AND {cs.scope_clause()}
)
SELECT
    day_idx,
    code,
    {cs.outcome_class_expr("outcome")} AS cls,
    count(*) AS n,
    COALESCE(sum(secs), 0) AS secs
FROM scoped
GROUP BY 1, 2, 3
"""
)


def _is_statement_timeout(exc: DBAPIError) -> bool:
    orig = exc.orig
    return (getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)) == "57014"


def _empty_split() -> dict:
    return {"calls": 0, "ai_completed": 0, "transferred": 0}


def _add(split: dict, cls: str, n: int) -> None:
    split["calls"] += n
    if cls in ("ai_completed", "transferred"):
        split[cls] += n


async def build_usage_report(
    query: UsageReportQuery, *, now: datetime | None = None
) -> dict:
    bounds = day_boundaries(query)
    params = {
        "bounds": bounds,
        "start": bounds[0],
        "end": bounds[-1],
        **cs.scope_params(now),
        **cs.category_params(query.codes),
        **cs.seconds_params(),
    }
    try:
        # Read-only: the autobegun transaction carries SET LOCAL and is rolled
        # back when the session closes.
        async with db_client.async_session() as session:
            await session.execute(
                text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            )
            rows = (await session.execute(_SQL, params)).all()
    except DBAPIError as exc:
        if _is_statement_timeout(exc):
            raise UsageReportTimeout() from None
        raise

    days = len(bounds) - 1
    daily = [_empty_split() for _ in range(days)]
    categories = {code: _empty_split() for code in query.codes}
    unclassified = _empty_split()
    outcomes = dict.fromkeys(OUTCOME_CLASSES, 0)
    calls, seconds = 0, 0.0
    for day_idx, code, cls, n, secs in rows:
        n = int(n)
        calls += n
        seconds += float(secs)
        outcomes[cls] += n
        _add(daily[day_idx - 1], cls, n)
        _add(categories[code] if code is not None else unclassified, cls, n)

    return {
        "calls": calls,
        "ai_seconds": round(seconds, 3),
        "outcomes": outcomes,
        "daily": [
            {"date": (query.date_from + timedelta(days=i)).isoformat(), **split}
            for i, split in enumerate(daily)
        ],
        "categories": categories,
        "unclassified": unclassified,
    }
