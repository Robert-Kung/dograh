"""Counting scope and outcome classes shared by the usage report and call records.

The usage report (ccp W4b) and the per-call records (ccp W4c) must agree to
the call: a list filtered by period, outcome or category has exactly as many
rows as the report's matching cell. Both therefore build their SQL from the
fragments here — never from a second copy.

Fragments take a column prefix (``"wr."`` or ``""``) so they can sit in a
join. Their bind parameters come from ``scope_params``, ``category_params``
and ``seconds_params``.
"""

import re
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo, available_timezones

EARLIEST = date(2020, 1, 1)
MAX_CODES = 20
STALE_AFTER = timedelta(hours=6)
OUTCOME_CLASSES = (
    "ai_completed",
    "transferred",
    "transfer_failed",
    "system_error",
    "unrecorded",
)
# One call never runs a day; a dirty row past this must not fail the report.
MAX_CALL_SECONDS = 86400
# whitespace Python's str.strip() removes, incl. the ideographic space
BLANK = " \t\n\r\f\v　"
_CODE_RE = re.compile(r"[a-z0-9_]{1,40}")  # fullmatch: `$` would let "x\n" through
# available_timezones() also lists these on Debian images: /etc/localtime's
# symlink and the "-00" placeholder — neither is a deployment's local time.
_NON_GEOGRAPHIC = frozenset({"localtime", "Factory", "posixrules"})


class ScopeInvalid(ValueError):
    """Input rejected; the message names the field, never echoes the value."""


@cache
def _zones() -> frozenset[str]:
    # available_timezones() walks the tz database on disk on every call
    return frozenset(available_timezones())


def is_valid_timezone(name: str) -> bool:
    return name not in _NON_GEOGRAPHIC and name in _zones()


def validate_period(
    date_from: date,
    date_to: date,
    timezone: str,
    *,
    max_days: int,
    now: datetime | None = None,
) -> None:
    if not is_valid_timezone(timezone):
        raise ScopeInvalid("timezone")
    if date_from < EARLIEST:
        raise ScopeInvalid("from")
    if date_from > date_to:
        raise ScopeInvalid("from")
    if (date_to - date_from).days + 1 > max_days:
        raise ScopeInvalid("to")
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone)).date()
    if date_to > today:
        raise ScopeInvalid("to")


def validate_codes(codes: list[str]) -> tuple[str, ...]:
    if len(codes) > MAX_CODES or len(set(codes)) != len(codes):
        raise ScopeInvalid("codes")
    if not all(isinstance(c, str) and _CODE_RE.fullmatch(c) for c in codes):
        raise ScopeInvalid("codes")
    return tuple(codes)


def day_boundaries(date_from: date, date_to: date, timezone: str) -> list[datetime]:
    """UTC instants of each local midnight from ``date_from`` to ``date_to + 1``."""
    tz = ZoneInfo(timezone)
    days = (date_to - date_from).days + 1
    return [
        datetime.combine(date_from + timedelta(days=i), time(), tz).astimezone(UTC)
        for i in range(days + 1)
    ]


def period_bounds(
    date_from: date, date_to: date, timezone: str
) -> tuple[datetime, datetime]:
    """``[start, end)`` in UTC for local days ``date_from``..``date_to``."""
    tz = ZoneInfo(timezone)
    start = datetime.combine(date_from, time(), tz).astimezone(UTC)
    end = datetime.combine(date_to + timedelta(days=1), time(), tz).astimezone(UTC)
    return start, end


def scope_clause(t: str = "") -> str:
    """Runs that count as an AI-reception call (W4b 設計 B).

    Ended runs only: completed, written a terminal outcome, or old enough that
    a killed worker never will. Binds ``:stale_before``.
    """
    return f"""(
        {t}mode = 'livekit'
        AND {t}call_type = 'inbound'
        AND (
            COALESCE({t}is_completed, false)
            OR starts_with({t}annotations->>'call_outcome', 'transferred:')
            OR {t}annotations->>'call_outcome' = 'safetynet_terminated'
            OR {t}created_at < :stale_before
        )
    )"""


def scope_params(now: datetime | None = None) -> dict:
    return {"stale_before": (now or datetime.now(UTC)) - STALE_AFTER}


def outcome_class_expr(outcome: str) -> str:
    """Five outcome classes (W4b 設計 C) over an SQL expression for call_outcome.

    transferred:safetynet is a system error, and the two setup-defect markers
    written at call start are "unrecorded", not a transfer attempt.
    """
    return f"""CASE
        WHEN {outcome} = 'ai_completed' THEN 'ai_completed'
        WHEN {outcome} IN ('safetynet_terminated', 'transferred:safetynet')
            THEN 'system_error'
        WHEN starts_with({outcome}, 'transferred:') THEN 'transferred'
        WHEN {outcome} IN ('transfer_failed:press0_not_installed',
                           'transfer_failed:config_unresolvable')
            THEN 'unrecorded'
        WHEN starts_with({outcome}, 'transfer_failed:') THEN 'transfer_failed'
        ELSE 'unrecorded'
    END"""


def category_expr(t: str = "") -> str:
    """Whitelisted category code or NULL (W4b 設計 E). Binds ``:codes``, ``:blank``.

    Unknown dispositions fold to NULL inside SQL, so a value outside the
    caller-supplied whitelist never leaves the database.
    """
    # btrim() alone strips only spaces; an LLM value often ends in a newline
    disp = f"lower(btrim({t}gathered_context->>'mapped_call_disposition', :blank))"
    return f"CASE WHEN {disp} = ANY(CAST(:codes AS text[])) THEN {disp} END"


def category_params(codes: tuple[str, ...] | list[str]) -> dict:
    return {"codes": list(codes), "blank": BLANK}


def seconds_expr(t: str = "") -> str:
    """AI seconds, non-numbers as 0, clamped to ``[0, :max_secs]``."""
    return f"""CASE WHEN json_typeof({t}usage_info->'call_duration_seconds') = 'number'
             THEN LEAST(GREATEST(({t}usage_info->>'call_duration_seconds')::double precision, 0),
                        :max_secs)
             ELSE 0 END"""


def seconds_params() -> dict:
    return {"max_secs": MAX_CALL_SECONDS}
