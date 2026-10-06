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

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from api.db import db_client

MAX_DAYS = 92
EARLIEST = date(2020, 1, 1)
MAX_CODES = 20
STALE_AFTER = timedelta(hours=6)
STATEMENT_TIMEOUT = "5s"
OUTCOME_CLASSES = (
    "ai_completed",
    "transferred",
    "transfer_failed",
    "system_error",
    "unrecorded",
)
_CODE_RE = re.compile(r"[a-z0-9_]{1,40}")  # fullmatch: `$` would let "x\n" through
# One call never runs a day; a dirty row past this must not fail the report.
MAX_CALL_SECONDS = 86400
# available_timezones() also lists these on Debian images: /etc/localtime's
# symlink and the "-00" placeholder — neither is a deployment's local time.
_NON_GEOGRAPHIC = frozenset({"localtime", "Factory", "posixrules"})


class UsageReportInvalid(ValueError):
    """Input rejected; the message names the field, never echoes the value."""


class UsageReportTimeout(Exception):
    """The statement timeout fired."""


@dataclass(frozen=True)
class UsageReportQuery:
    date_from: date
    date_to: date
    timezone: str
    codes: tuple[str, ...]


@cache
def _zones() -> frozenset[str]:
    # available_timezones() walks the tz database on disk on every call
    return frozenset(available_timezones())


def is_valid_timezone(name: str) -> bool:
    return name not in _NON_GEOGRAPHIC and name in _zones()


def validate_query(
    date_from: date,
    date_to: date,
    timezone: str,
    codes: list[str],
    *,
    now: datetime | None = None,
) -> UsageReportQuery:
    if not is_valid_timezone(timezone):
        raise UsageReportInvalid("timezone")
    if date_from < EARLIEST:
        raise UsageReportInvalid("from")
    if date_from > date_to:
        raise UsageReportInvalid("from")
    if (date_to - date_from).days + 1 > MAX_DAYS:
        raise UsageReportInvalid("to")
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone)).date()
    if date_to > today:
        raise UsageReportInvalid("to")
    if len(codes) > MAX_CODES or len(set(codes)) != len(codes):
        raise UsageReportInvalid("codes")
    if not all(_CODE_RE.fullmatch(code) for code in codes):
        raise UsageReportInvalid("codes")
    return UsageReportQuery(date_from, date_to, timezone, tuple(codes))


def day_boundaries(query: UsageReportQuery) -> list[datetime]:
    """UTC instants of each local midnight from ``date_from`` to ``date_to + 1``."""
    tz = ZoneInfo(query.timezone)
    days = (query.date_to - query.date_from).days + 1
    return [
        datetime.combine(query.date_from + timedelta(days=i), time(), tz).astimezone(
            UTC
        )
        for i in range(days + 1)
    ]


# Outcome classes (W4b 設計 C): transferred:safetynet is a system error, and the
# two setup-defect markers written at call start are "unrecorded", not a
# transfer attempt.
_SQL = text(
    """
WITH scoped AS (
    SELECT
        width_bucket(created_at, CAST(:bounds AS timestamptz[])) AS day_idx,
        annotations->>'call_outcome' AS outcome,
        CASE WHEN json_typeof(usage_info->'call_duration_seconds') = 'number'
             THEN LEAST(GREATEST((usage_info->>'call_duration_seconds')::double precision, 0),
                        :max_secs)
             ELSE 0 END AS secs,
        -- btrim() alone strips only spaces; an LLM value often ends in a newline
        CASE WHEN lower(btrim(gathered_context->>'mapped_call_disposition', :blank))
                  = ANY(CAST(:codes AS text[]))
             THEN lower(btrim(gathered_context->>'mapped_call_disposition', :blank))
        END AS code
    FROM workflow_runs
    WHERE mode = 'livekit'
      AND call_type = 'inbound'
      AND created_at >= :start AND created_at < :end
      AND (
          COALESCE(is_completed, false)
          OR starts_with(annotations->>'call_outcome', 'transferred:')
          OR annotations->>'call_outcome' = 'safetynet_terminated'
          OR created_at < :stale_before
      )
)
SELECT
    day_idx,
    code,
    CASE
        WHEN outcome = 'ai_completed' THEN 'ai_completed'
        WHEN outcome IN ('safetynet_terminated', 'transferred:safetynet')
            THEN 'system_error'
        WHEN starts_with(outcome, 'transferred:') THEN 'transferred'
        WHEN outcome IN ('transfer_failed:press0_not_installed',
                         'transfer_failed:config_unresolvable')
            THEN 'unrecorded'
        WHEN starts_with(outcome, 'transfer_failed:') THEN 'transfer_failed'
        ELSE 'unrecorded'
    END AS cls,
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
        "codes": list(query.codes),
        "start": bounds[0],
        "end": bounds[-1],
        "stale_before": (now or datetime.now(UTC)) - STALE_AFTER,
        "max_secs": MAX_CALL_SECONDS,
        # whitespace Python's str.strip() removes, incl. the ideographic space
        "blank": " \t\n\r\f\v\u3000",
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
