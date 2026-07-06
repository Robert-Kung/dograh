"""Business-hours gate (S-L3-PRESS0).

Shared service-layer pure function used by both the voice-triggered and the
press-0 (DTMF) cold-transfer paths to decide whether a human queue is open
before issuing a SIP REFER. Keeping it pure (``schedule`` + ``now`` in, bool
out) makes it trivially testable and reusable by S-L3-SAFETYNET.

Schedule shape (static weekly, from tool config)::

    {
        "tz": "Asia/Taipei",
        "mon": [["09:00", "18:00"]],
        "tue": [["09:00", "12:00"], ["13:00", "18:00"]],
        "fri": [["22:00", "02:00"]],   # wraps past midnight into Sat
        ...
    }

Per C4 (never dead air, route to a human), an absent or unparseable schedule
fails open — treated as currently open so the caller still reaches a human.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Monday=0 .. Sunday=6, matching datetime.weekday().
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _to_minutes(hhmm: str) -> int:
    """Parse ``"HH:MM"`` into minutes since midnight."""
    hours, minutes = hhmm.split(":")
    h, m = int(hours), int(minutes)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"time out of range: {hhmm!r}")
    return h * 60 + m


def _covered_same_day(intervals, t: int) -> bool:
    """True if minute-of-day ``t`` falls in any non-wrapping part of today's intervals.

    For a wrapping interval (end <= start, e.g. 22:00–02:00) only the pre-midnight
    span ``[start, 1440)`` is attributed to this day; the post-midnight remainder
    is handled by :func:`_covered_by_prev_day_wrap` against the following day.
    """
    for start, end in intervals:
        s, e = _to_minutes(start), _to_minutes(end)
        if s < e:
            if s <= t < e:
                return True
        elif e < s:  # wraps midnight; pre-midnight portion belongs to today
            if t >= s:
                return True
        # s == e is an empty interval — never open
    return False


def _covered_by_prev_day_wrap(intervals, t: int) -> bool:
    """True if a previous-day wrapping interval spills into ``t`` on the current day."""
    for start, end in intervals:
        s, e = _to_minutes(start), _to_minutes(end)
        if e < s and t < e:  # post-midnight remainder of yesterday's wrap
            return True
    return False


def is_open(schedule: dict | None, now: datetime) -> bool:
    """Return whether the queue is open at ``now`` per a static weekly ``schedule``.

    Args:
        schedule: Weekly schedule dict (see module docstring) or None/empty.
        now: The instant to evaluate. Tz-aware values are converted to the
            schedule's ``tz``; naive values are interpreted as wall-clock in
            the schedule's ``tz``.

    Returns:
        True if open (or if the schedule is absent/unparseable — fail open, C4).
    """
    if not schedule:
        return True

    try:
        tz_name = schedule.get("tz")
        if tz_name:
            tz = ZoneInfo(tz_name)
            now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)

        t = now.hour * 60 + now.minute
        today = _DAY_KEYS[now.weekday()]
        yesterday = _DAY_KEYS[(now.weekday() - 1) % 7]

        return _covered_same_day(
            schedule.get(today, []), t
        ) or _covered_by_prev_day_wrap(schedule.get(yesterday, []), t)
    except (ValueError, KeyError, TypeError) as e:
        logger.warning(
            "Unparseable business-hours schedule %r; failing open: %s", schedule, e
        )
        return True
